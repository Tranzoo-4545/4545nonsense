#!/usr/bin/env python3
"""
Lichess4545 Match Predictor using Glicko-2 ratings and Monte Carlo simulation.

Predicts team match outcomes based on individual player ratings.
Updates predictions as games complete throughout the week.

Usage:
    python match_predictor.py --season 48 --round 3
    python match_predictor.py --season 48 --round 3 --output predictions.html
    python match_predictor.py --season 48 --round 3 --simulations 50000
"""

import argparse
import json
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import requests

# Glicko-2 constants
GLICKO_SCALE = 173.7178
DEFAULT_RD = 350
DEFAULT_RATING = 1500

# Cache settings
CACHE_DIR = Path.home() / '.lichess4545_cache' / 'ratings'
CACHE_EXPIRY_HOURS = 24  # Ratings cached for 24 hours


@dataclass
class Player:
    username: str
    rating: int = DEFAULT_RATING
    rd: float = DEFAULT_RD
    
    @property
    def mu(self) -> float:
        """Convert to Glicko-2 scale."""
        return (self.rating - 1500) / GLICKO_SCALE
    
    @property
    def phi(self) -> float:
        """Convert RD to Glicko-2 scale."""
        return self.rd / GLICKO_SCALE


@dataclass
class BoardPairing:
    board: int
    white: Player
    black: Player
    white_team: str
    black_team: str
    game_id: Optional[str] = None
    result: Optional[float] = None  # 1 = white wins, 0.5 = draw, 0 = black wins, None = not played
    
    @property
    def is_played(self) -> bool:
        return self.result is not None


def g(phi: float) -> float:
    """Glicko-2 g function."""
    return 1 / math.sqrt(1 + 3 * phi**2 / math.pi**2)


def expected_score(player: Player, opponent: Player) -> float:
    """
    Calculate expected score for player against opponent using Glicko-2 formula.
    Returns probability of winning (0 to 1).
    """
    g_phi = g(opponent.phi)
    exponent = -g_phi * (player.mu - opponent.mu)
    return 1 / (1 + math.exp(exponent))


def calculate_draw_probability(white: Player, black: Player) -> float:
    """
    Calculate draw probability based on ratings.
    
    Calibrated from Lichess4545 seasons 40-48 (10,851 games, 15.8% overall draw rate).
    
    Focused on well-sampled data:
    - Rating 1600-2100 range (n > 900 per bucket)
    - Rating diff 0-150 range (n > 1000 per bucket)
    """
    avg_rating = (white.rating + black.rating) / 2
    rating_diff = abs(white.rating - black.rating)
    
    # Base draw rate scales with average rating
    # Fitted to: ~10% at 1600, ~15% at 1800, ~20% at 2000, ~25% at 2200
    base_draw_rate = 0.10 + (avg_rating - 1600) * 0.00025
    base_draw_rate = max(0.05, min(0.30, base_draw_rate))
    
    # Rating difference effect
    # Data shows relatively flat ~16% for diff 0-150, then drops
    # Using conservative slope to avoid overfitting to small samples at high diff
    relative_draw_rate = 1.0 - (rating_diff * 0.002)
    relative_draw_rate = max(0.20, min(1.0, relative_draw_rate))
    
    draw_prob = base_draw_rate * relative_draw_rate
    return max(0.03, min(0.35, draw_prob))


def simulate_game(white: Player, black: Player) -> float:
    """
    Simulate a single game outcome.
    Returns 1 (white wins), 0.5 (draw), or 0 (black wins).
    
    Uses Glicko expected score and rating-based draw probability.
    Includes ~25 Elo point advantage for White (first-move advantage).
    """
    # Create virtual "boosted" white player for expected score calculation
    # Research shows ~35 Elo advantage at GM level, ~20-30 at amateur level
    white_boosted = Player(
        username=white.username,
        rating=white.rating + 25,
        rd=white.rd
    )
    
    # Calculate expected score with White's first-move advantage
    white_expected = expected_score(white_boosted, black)
    
    # Calculate draw probability based on actual ratings (not boosted)
    draw_prob = calculate_draw_probability(white, black)
    
    # Adjust win probabilities accounting for draws
    # Expected score = P(win) + 0.5 * P(draw)
    # So: P(win) = expected_score - 0.5 * draw_prob (for white)
    # And remaining probability goes to black win
    white_win_prob = white_expected - 0.5 * draw_prob
    white_win_prob = max(0.0, min(1.0 - draw_prob, white_win_prob))
    
    black_win_prob = 1.0 - draw_prob - white_win_prob
    black_win_prob = max(0.0, black_win_prob)
    
    roll = random.random()
    if roll < white_win_prob:
        return 1.0
    elif roll < white_win_prob + draw_prob:
        return 0.5
    else:
        return 0.0


def simulate_match(pairings: list[BoardPairing], n_simulations: int = 10000) -> dict:
    """
    Run Monte Carlo simulation for a team match.
    
    Returns dict with:
    - team_a_expected: Expected score for team A
    - team_b_expected: Expected score for team B  
    - team_a_win_prob: Probability team A wins (>4 points)
    - team_b_win_prob: Probability team B wins (>4 points)
    - draw_prob: Probability of 4-4 tie
    - score_distribution: Dict of score -> probability
    - current_score: (team_a_actual, team_b_actual) for completed games
    - games_remaining: Number of games still to play
    """
    team_a = pairings[0].white_team
    team_b = pairings[0].black_team
    
    # Separate completed and pending games
    completed = [p for p in pairings if p.is_played]
    pending = [p for p in pairings if not p.is_played]
    
    # Calculate current score from completed games
    team_a_actual = 0.0
    team_b_actual = 0.0
    for p in completed:
        if p.white_team == team_a:
            team_a_actual += p.result
            team_b_actual += (1 - p.result)
        else:
            team_b_actual += p.result
            team_a_actual += (1 - p.result)
    
    # Calculate expected scores for pending games (with White advantage)
    board_expectations = []
    for p in pending:
        white_boosted = Player(username=p.white.username, rating=p.white.rating + 25, rd=p.white.rd)
        white_exp = expected_score(white_boosted, p.black)
        if p.white_team == team_a:
            board_expectations.append((white_exp, 1 - white_exp))  # (team_a_exp, team_b_exp)
        else:
            board_expectations.append((1 - white_exp, white_exp))
    
    # Expected totals
    team_a_expected = team_a_actual + sum(e[0] for e in board_expectations)
    team_b_expected = team_b_actual + sum(e[1] for e in board_expectations)
    
    # Monte Carlo simulation for pending games
    score_counts = defaultdict(int)
    team_a_wins = 0
    team_b_wins = 0
    draws = 0
    
    for _ in range(n_simulations):
        sim_a = team_a_actual
        sim_b = team_b_actual
        
        for p in pending:
            game_result = simulate_game(p.white, p.black)
            
            if p.white_team == team_a:
                sim_a += game_result
                sim_b += (1 - game_result)
            else:
                sim_b += game_result
                sim_a += (1 - game_result)
        
        # Round to nearest 0.5 for score key
        score_key = (sim_a, sim_b)
        score_counts[score_key] += 1
        
        if sim_a > sim_b:
            team_a_wins += 1
        elif sim_b > sim_a:
            team_b_wins += 1
        else:
            draws += 1
    
    # Convert counts to probabilities
    score_distribution = {k: v / n_simulations for k, v in sorted(score_counts.items())}
    
    return {
        'team_a': team_a,
        'team_b': team_b,
        'team_a_expected': team_a_expected,
        'team_b_expected': team_b_expected,
        'team_a_win_prob': team_a_wins / n_simulations,
        'team_b_win_prob': team_b_wins / n_simulations,
        'draw_prob': draws / n_simulations,
        'score_distribution': score_distribution,
        'current_score': (team_a_actual, team_b_actual),
        'games_remaining': len(pending),
        'board_expectations': board_expectations,
        'pairings': pairings,
    }


def simulate_single_match(pairings: list[BoardPairing]) -> tuple:
    """
    Simulate a single match outcome once.
    Returns (team_a, team_b, team_a_score, team_b_score).
    """
    team_a = pairings[0].white_team
    team_b = pairings[0].black_team
    
    team_a_score = 0.0
    team_b_score = 0.0
    
    for p in pairings:
        if p.is_played:
            # Use actual result
            if p.white_team == team_a:
                team_a_score += p.result
                team_b_score += (1 - p.result)
            else:
                team_b_score += p.result
                team_a_score += (1 - p.result)
        else:
            # Simulate
            game_result = simulate_game(p.white, p.black)
            
            if p.white_team == team_a:
                team_a_score += game_result
                team_b_score += (1 - game_result)
            else:
                team_b_score += game_result
                team_a_score += (1 - game_result)
    
    return (team_a, team_b, team_a_score, team_b_score)


def get_standings_before_round(season_data: dict, before_round: int) -> dict:
    """
    Calculate standings up to (but not including) a specific round.
    Returns dict: team_name -> {'match_points': int, 'game_points': float, 'games_won': int, 'sonneborn_berger': float}
    
    Uses same tiebreakers as standings.py:
    1. Match Points (desc)
    2. Game Points (desc)
    3. Games Won (desc)
    4. Sonneborn-Berger (desc)
    5. Team Name (alphabetical)
    """
    games = season_data.get('games', [])
    
    # Filter to games before the target round
    prior_games = [g for g in games if g.get('round', 0) < before_round]
    
    # Group by round and match
    rounds = defaultdict(list)
    for game in prior_games:
        round_num = game.get('round')
        if round_num:
            rounds[round_num].append(game)
    
    # Track team stats
    teams = defaultdict(lambda: {
        'match_points': 0, 
        'game_points': 0.0, 
        'games_won': 0,
        'matches': []  # List of (opponent, match_pts_earned) for SB calculation
    })
    
    for round_num in sorted(rounds.keys()):
        round_games = rounds[round_num]
        
        # Group by match
        matches = defaultdict(list)
        for game in round_games:
            white_team = game.get('white_team') or game.get('white_team_name', '')
            black_team = game.get('black_team') or game.get('black_team_name', '')
            if white_team and black_team:
                match_key = tuple(sorted([white_team, black_team]))
                matches[match_key].append(game)
        
        # Calculate match results
        for (team_a, team_b), match_games in matches.items():
            team_a_points = 0.0
            team_b_points = 0.0
            team_a_wins = 0
            team_b_wins = 0
            
            for game in match_games:
                white_team = game.get('white_team') or game.get('white_team_name')
                result = game.get('result', '')
                
                if result in ('1-0', '1X-0F'):
                    if white_team == team_a:
                        team_a_points += 1
                        team_a_wins += 1
                    else:
                        team_b_points += 1
                        team_b_wins += 1
                elif result in ('0-1', '0F-1X'):
                    if white_team == team_a:
                        team_b_points += 1
                        team_b_wins += 1
                    else:
                        team_a_points += 1
                        team_a_wins += 1
                elif result in ('1/2-1/2', '1/2Z-1/2Z', '0F-0F'):
                    team_a_points += 0.5
                    team_b_points += 0.5
            
            # Update game points and games won
            teams[team_a]['game_points'] += team_a_points
            teams[team_b]['game_points'] += team_b_points
            teams[team_a]['games_won'] += team_a_wins
            teams[team_b]['games_won'] += team_b_wins
            
            # Update match points and track for SB
            if team_a_points > team_b_points:
                teams[team_a]['match_points'] += 2
                teams[team_a]['matches'].append((team_b, 2))
                teams[team_b]['matches'].append((team_a, 0))
            elif team_b_points > team_a_points:
                teams[team_b]['match_points'] += 2
                teams[team_a]['matches'].append((team_b, 0))
                teams[team_b]['matches'].append((team_a, 2))
            else:
                teams[team_a]['match_points'] += 1
                teams[team_b]['match_points'] += 1
                teams[team_a]['matches'].append((team_b, 1))
                teams[team_b]['matches'].append((team_a, 1))
    
    # Calculate Sonneborn-Berger
    for team_name, team_data in teams.items():
        sb_score = 0.0
        for opponent, match_pts in team_data['matches']:
            opponent_match_pts = teams[opponent]['match_points']
            sb_score += match_pts * opponent_match_pts
        team_data['sonneborn_berger'] = sb_score
    
    # Convert to simple dict (remove matches list)
    result = {}
    for team_name, team_data in teams.items():
        result[team_name] = {
            'match_points': team_data['match_points'],
            'game_points': team_data['game_points'],
            'games_won': team_data['games_won'],
            'sonneborn_berger': team_data['sonneborn_berger'],
        }
    
    return result


def simulate_standings(all_matchups: list, standings_before: dict, n_simulations: int = 10000, capture_details: bool = False) -> tuple:
    """
    Run Monte Carlo simulation for all matches and track resulting standings.
    
    Returns tuple: (result_dict, detailed_sims)
    - result_dict: team_name -> {placement_probs, expected_placement, ...}
    - detailed_sims: list of simulation results if capture_details=True, else None
    """
    # Get all team names
    all_teams = set(standings_before.keys())
    for matchup in all_matchups:
        if matchup:
            all_teams.add(matchup[0].white_team)
            all_teams.add(matchup[0].black_team)
    
    all_teams = sorted(all_teams)  # Sort for consistent ordering
    
    # Build match labels for Excel headers
    match_labels = []
    for matchup in all_matchups:
        if matchup:
            team_a = matchup[0].white_team
            team_b = matchup[0].black_team
            # Normalize to always show alphabetically first team as "A"
            if team_a > team_b:
                team_a, team_b = team_b, team_a
            match_labels.append((team_a, team_b))
    
    # Track placement counts
    placement_counts = {team: defaultdict(int) for team in all_teams}
    total_match_points = {team: 0.0 for team in all_teams}
    total_game_points = {team: 0.0 for team in all_teams}
    
    # Detailed simulation storage
    detailed_sims = [] if capture_details else None
    
    for sim_idx in range(n_simulations):
        # Start with standings before round
        sim_standings = {team: dict(stats) for team, stats in standings_before.items()}
        
        # Ensure all teams exist
        for team in all_teams:
            if team not in sim_standings:
                sim_standings[team] = {'match_points': 0, 'game_points': 0.0}
        
        # Track match results for this simulation
        sim_match_results = [] if capture_details else None
        
        # Simulate each match
        for matchup in all_matchups:
            if not matchup:
                continue
            
            team_a, team_b, score_a, score_b = simulate_single_match(matchup)
            
            # Update game points
            sim_standings[team_a]['game_points'] += score_a
            sim_standings[team_b]['game_points'] += score_b
            
            # Update match points
            if score_a > score_b:
                sim_standings[team_a]['match_points'] += 2
            elif score_b > score_a:
                sim_standings[team_b]['match_points'] += 2
            else:
                sim_standings[team_a]['match_points'] += 1
                sim_standings[team_b]['match_points'] += 1
            
            if capture_details:
                # Store with consistent team ordering
                if team_a > team_b:
                    sim_match_results.append((score_b, score_a))
                else:
                    sim_match_results.append((score_a, score_b))
        
        # Sort to get placements
        sorted_teams = sorted(
            sim_standings.items(),
            key=lambda x: (-x[1]['match_points'], -x[1]['game_points'], x[0])
        )
        
        # Record placements and totals
        sim_placements = {}
        for place, (team, stats) in enumerate(sorted_teams, 1):
            placement_counts[team][place] += 1
            total_match_points[team] += stats['match_points']
            total_game_points[team] += stats['game_points']
            sim_placements[team] = place
        
        if capture_details:
            detailed_sims.append({
                'match_results': sim_match_results,
                'placements': sim_placements,
                'standings': {t: dict(s) for t, s in sim_standings.items()}
            })
    
    # Calculate current standings (before this round's games)
    # Use full tiebreakers: MP, GP, Games Won, SB, Name
    default_stats = {'match_points': 0, 'game_points': 0.0, 'games_won': 0, 'sonneborn_berger': 0.0}
    current_sorted = sorted(
        [(team, {**default_stats, **standings_before.get(team, {})}) 
         for team in all_teams],
        key=lambda x: (
            -x[1]['match_points'], 
            -x[1]['game_points'], 
            -x[1]['games_won'],
            -x[1]['sonneborn_berger'],
            x[0]
        )
    )
    current_placements = {team: i + 1 for i, (team, _) in enumerate(current_sorted)}
    
    # Build result
    result = {}
    for team in all_teams:
        placement_probs = {p: c / n_simulations for p, c in placement_counts[team].items()}
        expected_placement = sum(p * prob for p, prob in placement_probs.items())
        
        current_stats = standings_before.get(team, {'match_points': 0, 'game_points': 0.0})
        
        result[team] = {
            'current_standing': current_placements[team],
            'placement_probs': dict(sorted(placement_probs.items())),
            'expected_placement': expected_placement,
            'current_match_points': current_stats['match_points'],
            'current_game_points': current_stats['game_points'],
            'expected_match_points': total_match_points[team] / n_simulations,
            'expected_game_points': total_game_points[team] / n_simulations,
        }
    
    # Add metadata for Excel export
    if capture_details:
        detailed_sims = {
            'teams': all_teams,
            'match_labels': match_labels,
            'simulations': detailed_sims
        }
    
    return result, detailed_sims


def fetch_season_data(season: int) -> dict:
    """Fetch season data from Lichess4545 API."""
    url = f"https://www.lichess4545.com/api/get_season_games/?league=team4545&include_unplayed=true&season={season}"
    print(f"Fetching season {season} data...")
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def load_ratings_cache() -> dict:
    """Load cached ratings if they exist and are fresh."""
    cache_file = CACHE_DIR / 'player_ratings.json'
    if not cache_file.exists():
        return {}
    
    try:
        with open(cache_file, 'r') as f:
            data = json.load(f)
        
        # Check if cache is expired
        cached_time = datetime.fromisoformat(data.get('timestamp', '2000-01-01'))
        if datetime.now() - cached_time > timedelta(hours=CACHE_EXPIRY_HOURS):
            return {}
        
        return data.get('ratings', {})
    except Exception:
        return {}


def save_ratings_cache(ratings: dict):
    """Save ratings to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / 'player_ratings.json'
    
    # Convert Player objects to dicts for JSON serialization
    ratings_dict = {
        username: {'rating': p.rating, 'rd': p.rd}
        for username, p in ratings.items()
    }
    
    data = {
        'timestamp': datetime.now().isoformat(),
        'ratings': ratings_dict,
    }
    
    with open(cache_file, 'w') as f:
        json.dump(data, f)


def fetch_player_ratings(usernames: list[str], use_cache: bool = True) -> dict[str, Player]:
    """Fetch player ratings from Lichess API with caching."""
    players = {}
    
    # Load cache
    cached = {}
    if use_cache:
        cached_raw = load_ratings_cache()
        for username, data in cached_raw.items():
            cached[username] = Player(
                username=username,
                rating=data['rating'],
                rd=data['rd']
            )
    
    # Determine which players need fetching
    to_fetch = [u for u in usernames if u.lower() not in cached]
    
    if cached:
        print(f"  Using {len(usernames) - len(to_fetch)} cached ratings ({CACHE_EXPIRY_HOURS}h cache)")
    
    if to_fetch:
        print(f"  Fetching {len(to_fetch)} ratings from Lichess API (bulk)...")
        
        # Lichess bulk endpoint: POST /api/users with comma-separated usernames
        batch_size = 300
        fetched_count = 0
        
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i:i + batch_size]
            
            try:
                url = "https://lichess.org/api/users"
                response = requests.post(
                    url, 
                    data=",".join(batch),
                    headers={"Accept": "application/json"},
                    timeout=30
                )
                
                if response.status_code == 200:
                    users_data = response.json()
                    
                    for user_data in users_data:
                        username = (user_data.get('username') or user_data.get('id', '')).lower()
                        
                        # Get classical rating, fall back to rapid, then blitz
                        perfs = user_data.get('perfs', {})
                        classical = perfs.get('classical', {})
                        rapid = perfs.get('rapid', {})
                        blitz = perfs.get('blitz', {})
                        
                        if classical.get('games', 0) > 0:
                            rating = classical.get('rating', DEFAULT_RATING)
                            rd = classical.get('rd', DEFAULT_RD)
                        elif rapid.get('games', 0) > 0:
                            rating = rapid.get('rating', DEFAULT_RATING)
                            rd = rapid.get('rd', DEFAULT_RD)
                        elif blitz.get('games', 0) > 0:
                            rating = blitz.get('rating', DEFAULT_RATING)
                            rd = blitz.get('rd', DEFAULT_RD)
                        else:
                            rating = DEFAULT_RATING
                            rd = DEFAULT_RD
                        
                        players[username] = Player(username=username, rating=rating, rd=rd)
                        fetched_count += 1
                    
                    print(f"    Batch {i // batch_size + 1}: got {len(users_data)} ratings")
                else:
                    print(f"  Warning: Lichess API returned status {response.status_code}")
                    print(f"  Response: {response.text[:200]}")
                
                if i + batch_size < len(to_fetch):
                    time.sleep(1.0)  # Rate limiting between batches
                
            except requests.exceptions.RequestException as e:
                print(f"  Error: Failed to fetch ratings: {e}")
            except json.JSONDecodeError as e:
                print(f"  Error: Invalid JSON response: {e}")
            except Exception as e:
                print(f"  Warning: Unexpected error: {e}")
        
        print(f"  Successfully fetched {fetched_count} ratings")
    
    # Merge cached and freshly fetched
    for username in usernames:
        lower = username.lower()
        if lower in players:
            continue
        elif lower in cached:
            players[lower] = cached[lower]
        else:
            players[lower] = Player(username=lower)
    
    # Update cache with new data
    if to_fetch and use_cache:
        all_ratings = {**cached, **players}
        save_ratings_cache(all_ratings)
    
    print(f"  Got ratings for {len(players)} players")
    return players


def parse_result(result_str: str) -> Optional[float]:
    """Parse result string to float. Returns from white's perspective."""
    if not result_str:
        return None
    result_str = result_str.strip()
    
    # White wins (including forfeit wins)
    if result_str in ('1-0', '1X-0F'):
        return 1.0
    # Black wins (including forfeit wins)
    elif result_str in ('0-1', '0F-1X'):
        return 0.0
    # Draws (including scheduling draws)
    elif result_str in ('1/2-1/2', '1/2Z-1/2Z'):
        return 0.5
    # Double forfeit
    elif result_str == '0F-0F':
        return 0.5  # Treat as draw for scoring purposes
    
    return None


def get_round_pairings(season_data: dict, round_num: int, player_ratings: dict) -> list[dict]:
    """
    Extract pairings grouped by team matchup.
    Returns list of team matchups, each containing board pairings.
    """
    games = season_data.get('games', [])
    round_games = [g for g in games if g.get('round') == round_num]
    
    if not round_games:
        print(f"  Warning: No games found for round {round_num}")
        return []
    
    # Group by team matchup
    matchups = defaultdict(list)
    for game in round_games:
        white = game.get('white', '').lower()
        black = game.get('black', '').lower()
        white_team = game.get('white_team') or game.get('white_team_name', '')
        black_team = game.get('black_team') or game.get('black_team_name', '')
        
        # Board number might be under different keys
        board = game.get('board') or game.get('board_number') or game.get('board_num', 0)
        if isinstance(board, str):
            try:
                board = int(board)
            except ValueError:
                board = 0
        
        game_id = game.get('game_id') or (game.get('game_link', '').split('/')[-1] if game.get('game_link') else None)
        result_str = game.get('result', '')
        
        # Parse result (works even without game_id for forfeits)
        result = parse_result(result_str)
        
        # Get player objects
        white_player = player_ratings.get(white, Player(username=white))
        black_player = player_ratings.get(black, Player(username=black))
        
        pairing = BoardPairing(
            board=board,
            white=white_player,
            black=black_player,
            white_team=white_team,
            black_team=black_team,
            game_id=game_id,
            result=result,
        )
        
        matchup_key = tuple(sorted([white_team, black_team]))
        matchups[matchup_key].append(pairing)
    
    # Sort each matchup by board number, or by rating if boards are unknown
    result = []
    for key, pairings in matchups.items():
        # If all boards are 0/missing, assign by average rating (highest = board 1)
        if all(p.board == 0 for p in pairings):
            # Sort by average rating descending (board 1 = highest rated)
            pairings.sort(key=lambda p: -(p.white.rating + p.black.rating) / 2)
            for i, p in enumerate(pairings):
                p.board = i + 1
        else:
            pairings.sort(key=lambda p: p.board)
        result.append(pairings)
    
    return result


def get_common_css() -> str:
    """Return common CSS used across all reports."""
    return '''
        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-card: #21262d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --border-color: #30363d;
            --team-a-color: #58a6ff;
            --team-b-color: #f78166;
            --win-color: #3fb950;
            --draw-color: #d29922;
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            padding: 2rem;
            line-height: 1.5;
        }
        
        h1 {
            text-align: center;
            margin-bottom: 0.5rem;
            font-size: 1.8rem;
        }
        
        .subtitle {
            text-align: center;
            color: var(--text-secondary);
            margin-bottom: 2rem;
        }
'''


def generate_matches_html(predictions: list[dict], season: int, round_num: int) -> str:
    """Generate HTML report with match predictions."""
    
    # Get current UTC time
    utc_now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    # Sort by team A win probability (most uncertain matches first for drama)
    predictions.sort(key=lambda p: abs(p['team_a_win_prob'] - 0.5))
    
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Match Predictions - Season {season} Round {round_num}</title>
    <style>
        {get_common_css()}
        
        .predictions-container {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }}
        
        .match-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
        }}
        
        .match-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem 1.5rem;
            background: var(--bg-card);
            border-bottom: 1px solid var(--border-color);
        }}
        
        .team {{
            font-weight: 600;
            font-size: 1.1rem;
            max-width: 35%;
        }}
        
        .team-a {{ color: var(--team-a-color); }}
        .team-b {{ color: var(--team-b-color); text-align: right; }}
        
        .vs {{
            color: var(--text-secondary);
            font-size: 0.9rem;
        }}
        
        .prediction-bar {{
            display: flex;
            height: 40px;
            margin: 1rem 1.5rem;
            border-radius: 6px;
            overflow: hidden;
        }}
        
        .prob-segment {{
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 0.85rem;
            color: white;
            min-width: 40px;
        }}
        
        .prob-a {{ background: var(--team-a-color); }}
        .prob-draw {{ background: var(--draw-color); }}
        .prob-b {{ background: var(--team-b-color); }}
        
        .match-details {{
            padding: 1rem 1.5rem;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
        }}
        
        .detail-section {{
            background: var(--bg-card);
            border-radius: 8px;
            padding: 0.75rem 1rem;
        }}
        
        .detail-title {{
            font-size: 0.75rem;
            color: var(--text-secondary);
            margin-bottom: 0.5rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .expected-score {{
            font-size: 1.5rem;
            font-weight: 600;
        }}
        
        .current-score {{
            font-size: 1.2rem;
            color: var(--win-color);
        }}
        
        .boards-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 0.5rem;
            padding: 0 1.5rem 1rem;
        }}
        
        .board-card {{
            background: var(--bg-card);
            border-radius: 6px;
            padding: 0.5rem;
            font-size: 0.75rem;
        }}
        
        .board-players {{
            display: flex;
            flex-direction: column;
            gap: 0.2rem;
        }}
        
        .board-player {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .player-name {{
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 70%;
        }}
        
        .player-rating {{
            color: var(--text-secondary);
            font-size: 0.7rem;
        }}
        
        .color-indicator {{
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 2px;
            margin-right: 0.35rem;
            vertical-align: middle;
        }}
        
        .color-white {{
            background: #fff;
            border: 1px solid #888;
        }}
        
        .color-black {{
            background: #333;
            border: 1px solid #333;
        }}
        
        .score-result {{
            font-weight: 700;
            font-size: 0.85rem;
            min-width: 1.5rem;
            text-align: center;
        }}
        
        .score-prediction {{
            font-size: 0.7rem;
            min-width: 2rem;
            text-align: center;
            opacity: 0.8;
        }}
        
        .win-prob {{
            font-weight: 600;
            font-size: 0.7rem;
        }}
        
        .board-result {{
            text-align: center;
            font-weight: 600;
            margin-top: 0.25rem;
            padding-top: 0.25rem;
            border-top: 1px solid var(--border-color);
        }}
        
        .result-played {{ color: var(--win-color); }}
        .result-pending {{ color: var(--text-secondary); }}
        
        @media (max-width: 768px) {{
            .boards-grid {{ grid-template-columns: repeat(2, 1fr); }}
            .match-details {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <h1>🎯 Match Predictions</h1>
    <p class="subtitle">Season {season} Round {round_num} • Monte Carlo Simulation • Generated: {utc_now}</p>
    
    <div class="predictions-container">
'''
    
    for pred in predictions:
        team_a = pred['team_a']
        team_b = pred['team_b']
        a_prob = pred['team_a_win_prob'] * 100
        b_prob = pred['team_b_win_prob'] * 100
        draw_prob = pred['draw_prob'] * 100
        a_exp = pred['team_a_expected']
        b_exp = pred['team_b_expected']
        a_curr, b_curr = pred['current_score']
        remaining = pred['games_remaining']
        
        html += f'''
        <div class="match-card">
            <div class="match-header">
                <div class="team team-a">{team_a}</div>
                <div class="vs">vs</div>
                <div class="team team-b">{team_b}</div>
            </div>
            
            <div class="prediction-bar">
                <div class="prob-segment prob-a" style="width: {a_prob}%">{a_prob:.0f}%</div>
                <div class="prob-segment prob-draw" style="width: {draw_prob}%">{draw_prob:.0f}%</div>
                <div class="prob-segment prob-b" style="width: {b_prob}%">{b_prob:.0f}%</div>
            </div>
            
            <div class="match-details">
                <div class="detail-section">
                    <div class="detail-title">Expected Final Score</div>
                    <div class="expected-score">
                        <span style="color: var(--team-a-color)">{a_exp:.1f}</span>
                        <span style="color: var(--text-secondary)"> - </span>
                        <span style="color: var(--team-b-color)">{b_exp:.1f}</span>
                    </div>
                </div>
                <div class="detail-section">
                    <div class="detail-title">Current Score ({8 - remaining}/8 played)</div>
                    <div class="current-score">
                        {a_curr:.1f} - {b_curr:.1f}
                    </div>
                </div>
            </div>
            
            <div class="boards-grid">
'''
        
        for pairing in pred['pairings']:
            white = pairing.white
            black = pairing.black
            white_boosted = Player(username=white.username, rating=white.rating + 25, rd=white.rd)
            white_exp = expected_score(white_boosted, black) * 100
            black_exp = 100 - white_exp
            
            # Determine individual scores/display for each player
            if pairing.is_played:
                if pairing.result == 1:  # White won
                    white_score, black_score = "1", "0"
                elif pairing.result == 0:  # Black won
                    white_score, black_score = "0", "1"
                else:  # Draw
                    white_score, black_score = "½", "½"
                show_result = True
            else:
                white_score = f"{white_exp:.0f}%"
                black_score = f"{black_exp:.0f}%"
                show_result = False
            
            # Determine which player is team A and their color indicator
            if pairing.white_team == team_a:
                a_player, b_player = white, black
                a_score, b_score = white_score, black_score
                a_color_class = "color-white"
                b_color_class = "color-black"
            else:
                a_player, b_player = black, white
                a_score, b_score = black_score, white_score
                a_color_class = "color-black"
                b_color_class = "color-white"
            
            # Style for scores
            a_score_class = "score-result" if show_result else "score-prediction"
            b_score_class = "score-result" if show_result else "score-prediction"
            
            html += f'''
                <div class="board-card">
                    <div class="board-players">
                        <div class="board-player">
                            <span class="color-indicator {a_color_class}"></span>
                            <span class="player-name" style="color: var(--team-a-color)">{a_player.username}</span>
                            <span class="{a_score_class}" style="color: var(--team-a-color)">{a_score}</span>
                            <span class="player-rating">{a_player.rating}</span>
                        </div>
                        <div class="board-player">
                            <span class="color-indicator {b_color_class}"></span>
                            <span class="player-name" style="color: var(--team-b-color)">{b_player.username}</span>
                            <span class="{b_score_class}" style="color: var(--team-b-color)">{b_score}</span>
                            <span class="player-rating">{b_player.rating}</span>
                        </div>
                    </div>
                </div>
'''
        
        html += '''
            </div>
        </div>
'''
    
    html += '''
    </div>
</body>
</html>
'''
    
    return html


def generate_standings_html(standings_prediction: dict, season: int, round_num: int) -> str:
    """Generate HTML report with predicted standings table."""
    
    # Get current UTC time
    utc_now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    sorted_standings = sorted(
        standings_prediction.items(),
        key=lambda x: x[1]['expected_placement']
    )
    
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Predicted Standings - Season {season} Round {round_num}</title>
    <style>
        {get_common_css()}
    </style>
</head>
<body>
    <h1>📊 Predicted Standings After Round {round_num}</h1>
    <p class="subtitle">Season {season} • Monte Carlo Simulation • Generated: {utc_now}</p>
    
    <div style="max-width: 800px; margin: 1.5rem auto; background: var(--bg-secondary); border-radius: 12px; overflow: hidden; border: 1px solid var(--border-color);">
        <table style="width: 100%; border-collapse: collapse; font-size: 0.9rem;">
            <thead>
                <tr style="background: var(--bg-card); color: var(--text-secondary); text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em;">
                    <th style="padding: 0.75rem; text-align: center; width: 40px;">#</th>
                    <th style="padding: 0.75rem; text-align: left;">Team</th>
                    <th style="padding: 0.75rem; text-align: center;">Now</th>
                    <th style="padding: 0.75rem; text-align: center;">1st</th>
                    <th style="padding: 0.75rem; text-align: center;">Top 3</th>
                    <th style="padding: 0.75rem; text-align: center;">MP</th>
                    <th style="padding: 0.75rem; text-align: center;">GP</th>
                </tr>
            </thead>
            <tbody>
'''
    
    for i, (team, stats) in enumerate(sorted_standings, 1):
        top3_prob = sum(stats['placement_probs'].get(j, 0) for j in range(1, 4)) * 100
        top1_prob = stats['placement_probs'].get(1, 0) * 100
        current = stats['current_standing']
        
        row_bg = 'var(--bg-card)' if i % 2 == 1 else 'transparent'
        rank_color = 'var(--win-color)' if i <= 3 else 'var(--text-primary)'
        
        html += f'''
                <tr style="background: {row_bg}; border-bottom: 1px solid var(--border-color);">
                    <td style="padding: 0.6rem; text-align: center; color: {rank_color}; font-weight: 600;">{i}</td>
                    <td style="padding: 0.6rem 0.75rem; color: var(--text-primary); font-weight: 500;">{team}</td>
                    <td style="padding: 0.6rem; text-align: center; color: var(--text-secondary);">{current}</td>
                    <td style="padding: 0.6rem; text-align: center; color: {'var(--win-color)' if top1_prob > 20 else 'var(--text-secondary)'}; font-weight: {'600' if top1_prob > 20 else '400'};">{top1_prob:.0f}%</td>
                    <td style="padding: 0.6rem; text-align: center; color: {'var(--win-color)' if top3_prob > 50 else 'var(--text-secondary)'};">{top3_prob:.0f}%</td>
                    <td style="padding: 0.6rem; text-align: center; color: var(--text-secondary);">{stats['expected_match_points']:.1f}</td>
                    <td style="padding: 0.6rem; text-align: center; color: var(--text-secondary);">{stats['expected_game_points']:.1f}</td>
                </tr>
'''
    
    html += '''
            </tbody>
        </table>
    </div>
</body>
</html>
'''
    
    return html


def generate_distribution_html(standings_prediction: dict, season: int, round_num: int) -> str:
    """Generate HTML report with placement probability distribution for each team."""
    
    # Get current UTC time
    utc_now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    
    sorted_standings = sorted(
        standings_prediction.items(),
        key=lambda x: x[1]['expected_placement']
    )
    
    num_teams = len(sorted_standings)
    
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Placement Distribution - Season {season} Round {round_num}</title>
    <style>
        {get_common_css()}
        
        .distribution-container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        
        .heatmap {{
            overflow-x: auto;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
        }}
        
        th, td {{
            padding: 0.5rem;
            text-align: center;
            border: 1px solid var(--border-color);
        }}
        
        th {{
            background: var(--bg-card);
            color: var(--text-secondary);
            font-weight: 600;
        }}
        
        .team-name {{
            text-align: left;
            padding-left: 0.75rem;
            white-space: nowrap;
            background: var(--bg-card);
            color: var(--text-primary);
            font-weight: 500;
        }}
        
        .prob-cell {{
            font-weight: 500;
            min-width: 45px;
        }}
        
        .team-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
            gap: 1.5rem;
            margin-top: 1.5rem;
        }}
        
        .team-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1rem;
        }}
        
        .team-card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }}
        
        .team-card-name {{
            font-weight: 600;
            font-size: 1rem;
            color: var(--text-primary);
        }}
        
        .team-card-stats {{
            font-size: 0.8rem;
            color: var(--text-secondary);
        }}
        
        .team-chart {{
            width: 100%;
            height: auto;
            display: block;
        }}
        
        .team-card-footer {{
            display: flex;
            gap: 0.75rem;
            margin-top: 0.75rem;
        }}
        
        .prob-badge {{
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 500;
            color: var(--text-primary);
        }}
        
        @media (max-width: 900px) {{
            .team-cards {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <h1>📈 Placement Probability Distribution</h1>
    <p class="subtitle">Season {season} Round {round_num} • Probability of finishing in each position • Generated: {utc_now}</p>
    
    <div class="distribution-container">
        <div class="heatmap">
            <table>
                <thead>
                    <tr>
                        <th style="width: 40px;">#</th>
                        <th style="text-align: left; padding-left: 0.75rem;">Team</th>
                        <th style="width: 50px;">Now</th>
'''
    
    # Column headers for positions
    for pos in range(1, num_teams + 1):
        html += f'                        <th>{pos}</th>\n'
    
    html += '''                    </tr>
                </thead>
                <tbody>
'''
    
    # Rows for each team
    for i, (team, stats) in enumerate(sorted_standings, 1):
        current = stats['current_standing']
        html += f'                    <tr>\n'
        html += f'                        <td class="prob-cell" style="background: var(--bg-card); color: var(--win-color); font-weight: 600;">{i}</td>\n'
        html += f'                        <td class="team-name">{team}</td>\n'
        html += f'                        <td class="prob-cell" style="background: var(--bg-card); color: var(--text-secondary);">{current}</td>\n'
        
        for pos in range(1, num_teams + 1):
            prob = stats['placement_probs'].get(pos, 0) * 100
            
            # Color intensity based on probability
            if prob >= 50:
                bg_color = 'rgba(63, 185, 80, 0.8)'  # Strong green
                text_color = 'white'
            elif prob >= 30:
                bg_color = 'rgba(63, 185, 80, 0.5)'  # Medium green
                text_color = 'white'
            elif prob >= 15:
                bg_color = 'rgba(63, 185, 80, 0.3)'  # Light green
                text_color = 'var(--text-primary)'
            elif prob >= 5:
                bg_color = 'rgba(139, 148, 158, 0.2)'  # Subtle gray
                text_color = 'var(--text-secondary)'
            else:
                bg_color = 'transparent'
                text_color = 'var(--text-secondary)'
            
            prob_str = f'{prob:.0f}%' if prob >= 1 else ('—' if prob == 0 else '<1%')
            
            html += f'                        <td class="prob-cell" style="background: {bg_color}; color: {text_color};">{prob_str}</td>\n'
        
        html += '                    </tr>\n'
    
    html += '''                </tbody>
            </table>
        </div>
        
        <h2 style="margin-top: 3rem; color: var(--text-secondary);">Individual Team Distributions</h2>
        <div class="team-cards">
'''
    
    # Generate individual team cards with bar charts
    for team, stats in sorted_standings:
        current = stats['current_standing']
        probs = stats['placement_probs']
        
        # Find most likely position (mode)
        if probs:
            most_likely_pos = max(probs.keys(), key=lambda p: probs[p])
            most_likely_prob = probs[most_likely_pos] * 100
        else:
            most_likely_pos = current
            most_likely_prob = 0
        
        # Find max probability for scaling
        max_prob = max(probs.values()) if probs else 0.01
        
        # Find the team's "hot zone" - positions with significant probability
        significant_positions = [p for p, prob in probs.items() if prob >= 0.02]
        if significant_positions:
            best_pos = min(significant_positions)
            worst_pos = max(significant_positions)
        else:
            best_pos = worst_pos = int(expected)
        
        # Generate SVG bar chart
        chart_width = 400
        chart_height = 120
        bar_width = chart_width / num_teams - 4
        
        svg_bars = ''
        svg_labels = ''
        for pos in range(1, num_teams + 1):
            prob = probs.get(pos, 0)
            bar_height = (prob / max_prob) * 80 if max_prob > 0 else 0
            x = (pos - 1) * (chart_width / num_teams) + 2
            y = 90 - bar_height
            
            # Color based on relative position within team's range
            if prob >= max_prob * 0.8:
                fill = '#3fb950'  # Best positions for this team
                opacity = 1.0
            elif prob >= max_prob * 0.5:
                fill = '#58a6ff'  # Good positions
                opacity = 0.9
            elif prob >= max_prob * 0.2:
                fill = '#d29922'  # Medium positions
                opacity = 0.8
            elif prob > 0:
                fill = '#8b949e'  # Tail positions
                opacity = 0.5
            else:
                fill = '#8b949e'
                opacity = 0.2
            
            if bar_height > 0:
                svg_bars += f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" fill="{fill}" opacity="{opacity}" rx="2"/>'
            
            # Label only for bars with significant probability (>5%)
            if prob >= 0.05 and bar_height > 12:
                label_y = y - 4
                prob_text = f'{prob*100:.0f}'
                svg_bars += f'<text x="{x + bar_width/2}" y="{label_y}" text-anchor="middle" font-size="9" fill="var(--text-secondary)">{prob_text}</text>'
            
            # Position labels - only show 1, 5, 10, 15, 20, etc. or last position
            if pos == 1 or pos % 5 == 0 or pos == num_teams:
                svg_labels += f'<text x="{x + bar_width/2}" y="105" text-anchor="middle" font-size="10" fill="var(--text-secondary)">{pos}</text>'
        
        # Top 3 probability
        top3_prob = sum(probs.get(j, 0) for j in range(1, 4)) * 100
        top1_prob = probs.get(1, 0) * 100
        
        html += f'''
            <div class="team-card">
                <div class="team-card-header">
                    <span class="team-card-name">{team}</span>
                    <span class="team-card-stats">Now: {current} → Likely: {most_likely_pos}</span>
                </div>
                <svg viewBox="0 0 {chart_width} {chart_height}" class="team-chart">
                    {svg_bars}
                    {svg_labels}
                </svg>
                <div class="team-card-footer">
                    <span class="prob-badge" style="background: {'rgba(63,185,80,0.3)' if top1_prob > 10 else 'rgba(139,148,158,0.2)'}">🥇 {top1_prob:.0f}%</span>
                    <span class="prob-badge" style="background: {'rgba(63,185,80,0.2)' if top3_prob > 30 else 'rgba(139,148,158,0.2)'}">🏆 Top 3: {top3_prob:.0f}%</span>
                </div>
            </div>
'''
    
    html += '''
        </div>
    </div>
</body>
</html>
'''
    
    return html


def generate_html_report(predictions: list[dict], season: int, round_num: int, standings_prediction: dict = None) -> str:
    """Generate combined HTML report (legacy, kept for compatibility)."""
    html = generate_matches_html(predictions, season, round_num)
    # Note: This doesn't include standings anymore - use separate files
    return html


def export_simulations_to_excel(detailed_sims: dict, filepath: str, season: int, round_num: int):
    """Export individual simulation results to Excel file."""
    import pandas as pd
    
    teams = detailed_sims['teams']
    match_labels = detailed_sims['match_labels']
    simulations = detailed_sims['simulations']
    
    # Build match results DataFrame
    match_data = []
    for sim_idx, sim in enumerate(simulations):
        row = {'Simulation': sim_idx + 1}
        for match_idx, (team_a, team_b) in enumerate(match_labels):
            score_a, score_b = sim['match_results'][match_idx]
            row[f'{team_a[:15]} vs {team_b[:15]}'] = f'{score_a}-{score_b}'
        match_data.append(row)
    
    df_matches = pd.DataFrame(match_data)
    
    # Build standings DataFrame
    standings_data = []
    for sim_idx, sim in enumerate(simulations):
        row = {'Simulation': sim_idx + 1}
        for team in teams:
            row[f'{team[:20]}_Pos'] = sim['placements'][team]
            row[f'{team[:20]}_MP'] = sim['standings'][team]['match_points']
            row[f'{team[:20]}_GP'] = sim['standings'][team]['game_points']
        standings_data.append(row)
    
    df_standings = pd.DataFrame(standings_data)
    
    # Write to Excel with multiple sheets
    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        df_matches.to_excel(writer, sheet_name='Match Results', index=False)
        df_standings.to_excel(writer, sheet_name='Standings', index=False)
        
        # Add summary sheet
        summary_data = {
            'Info': ['Season', 'Round', 'Simulations', 'Teams', 'Matches'],
            'Value': [season, round_num, len(simulations), len(teams), len(match_labels)]
        }
        df_summary = pd.DataFrame(summary_data)
        df_summary.to_excel(writer, sheet_name='Summary', index=False)
    
    print(f"✅ Simulation details exported to: {filepath}")


def generate_swiss_pairings(
    standings: dict,
    matchup_history: set,
    color_history: dict,
    team_rosters: dict,
    player_ratings: dict
) -> list:
    """
    Generate Swiss-style pairings for the next round.
    
    Args:
        standings: {team: {'mp': match_points, 'gp': game_points}}
        matchup_history: set of (team_a, team_b) tuples (sorted alphabetically)
        color_history: {team: 'white' or 'black'} - who had White on board 1 last round
        team_rosters: {team: [player1, player2, ...]} - ordered by board
        player_ratings: {username: Player}
    
    Returns:
        List of match pairings, each being a list of BoardPairing objects
    """
    # Sort teams by match points (desc), then game points (desc)
    sorted_teams = sorted(
        standings.keys(),
        key=lambda t: (-standings[t]['match_points'], -standings[t]['game_points'])
    )
    
    # Group by match points
    score_groups = {}
    for team in sorted_teams:
        mp = standings[team]['match_points']
        if mp not in score_groups:
            score_groups[mp] = []
        score_groups[mp].append(team)
    
    # Process groups from highest to lowest score
    all_pairings = []
    floaters = []
    
    for mp in sorted(score_groups.keys(), reverse=True):
        teams = floaters + score_groups[mp]
        floaters = []
        
        while len(teams) >= 2:
            team_a = teams.pop(0)  # Top team in group
            
            # Find valid opponent (not played before)
            paired = False
            for i, team_b in enumerate(teams):
                matchup_key = tuple(sorted([team_a, team_b]))
                if matchup_key not in matchup_history:
                    teams.pop(i)
                    
                    # Determine colors - alternate from last round
                    # Team that had black last round gets white this round
                    if color_history.get(team_a) == 'black':
                        white_team, black_team = team_a, team_b
                    elif color_history.get(team_b) == 'black':
                        white_team, black_team = team_b, team_a
                    else:
                        # No history - randomly assign (or alphabetically for consistency)
                        white_team, black_team = sorted([team_a, team_b])
                    
                    # Create board pairings
                    match_pairings = create_board_pairings(
                        white_team, black_team, team_rosters, player_ratings
                    )
                    all_pairings.append(match_pairings)
                    paired = True
                    break
            
            if not paired:
                # No valid opponent - float down
                floaters.append(team_a)
        
        # Any remaining teams float down
        floaters.extend(teams)
    
    # Handle any leftover floaters (shouldn't happen with even teams)
    if len(floaters) >= 2:
        # Pair remaining teams even if they've played before
        while len(floaters) >= 2:
            team_a = floaters.pop(0)
            team_b = floaters.pop(0)
            white_team, black_team = sorted([team_a, team_b])
            match_pairings = create_board_pairings(
                white_team, black_team, team_rosters, player_ratings
            )
            all_pairings.append(match_pairings)
    
    return all_pairings


def create_board_pairings(
    white_team: str,
    black_team: str,
    team_rosters: dict,
    player_ratings: dict
) -> list:
    """Create board pairings for a team matchup."""
    white_roster = team_rosters.get(white_team, [])
    black_roster = team_rosters.get(black_team, [])
    
    pairings = []
    for board in range(min(len(white_roster), len(black_roster))):
        white_username = white_roster[board]
        black_username = black_roster[board]
        
        white_player = player_ratings.get(white_username, Player(username=white_username))
        black_player = player_ratings.get(black_username, Player(username=black_username))
        
        pairing = BoardPairing(
            board=board + 1,
            white=white_player,
            black=black_player,
            white_team=white_team,
            black_team=black_team,
            game_id=None,
            result=None
        )
        pairings.append(pairing)
    
    return pairings


def simulate_full_season(
    season_data: dict,
    current_round: int,
    player_ratings: dict,
    num_simulations: int = 10000,
    final_round: int = 8
) -> dict:
    """
    Simulate the entire remaining season using Swiss pairings.
    
    Returns:
        standings_prediction: {team: {placement_probs, expected_placement, ...}}
    """
    import sys
    
    games = season_data.get('games', [])
    
    # Get current matchups for this round
    current_matchups = get_round_pairings(season_data, current_round, player_ratings)
    
    # Extract team rosters from current round (assume stable for future rounds)
    team_rosters = {}
    for matchup in current_matchups:
        for pairing in matchup:
            if pairing.white_team not in team_rosters:
                team_rosters[pairing.white_team] = []
            if pairing.black_team not in team_rosters:
                team_rosters[pairing.black_team] = []
    
    # Fill rosters ordered by board
    for matchup in current_matchups:
        matchup_sorted = sorted(matchup, key=lambda p: p.board)
        for pairing in matchup_sorted:
            if len(team_rosters[pairing.white_team]) < 8:
                team_rosters[pairing.white_team].append(pairing.white.username)
            if len(team_rosters[pairing.black_team]) < 8:
                team_rosters[pairing.black_team].append(pairing.black.username)
    
    # Get standings before current round
    standings_before = get_standings_before_round(season_data, current_round)
    teams = list(standings_before.keys())
    
    # Build historical matchup set (rounds before current)
    historical_matchups = set()
    for round_num in range(1, current_round):
        round_games = [g for g in games if g.get('round') == round_num]
        for game in round_games:
            white_team = game.get('white_team') or game.get('white_team_name', '')
            black_team = game.get('black_team') or game.get('black_team_name', '')
            if white_team and black_team:
                matchup_key = tuple(sorted([white_team, black_team]))
                historical_matchups.add(matchup_key)
    
    # Track placement counts across all simulations
    placement_counts = {team: defaultdict(int) for team in teams}
    total_placements = {team: 0.0 for team in teams}
    total_match_points = {team: 0.0 for team in teams}
    total_game_points = {team: 0.0 for team in teams}
    
    # Calculate current standings for display
    # Use full tiebreakers: MP, GP, Games Won, SB, Name
    default_stats = {'match_points': 0, 'game_points': 0.0, 'games_won': 0, 'sonneborn_berger': 0.0}
    current_sorted = sorted(
        [(team, {**default_stats, **standings_before.get(team, {})}) 
         for team in teams],
        key=lambda x: (
            -x[1]['match_points'], 
            -x[1]['game_points'], 
            -x[1]['games_won'],
            -x[1]['sonneborn_berger'],
            x[0]
        )
    )
    current_placements = {team: i + 1 for i, (team, _) in enumerate(current_sorted)}
    
    rounds_to_simulate = final_round - current_round + 1
    
    print(f"\nSimulating {rounds_to_simulate} rounds ({current_round} through {final_round})...")
    print(f"Running {num_simulations:,} full-season simulations\n")
    
    progress_interval = max(1, num_simulations // 20)
    
    for sim in range(num_simulations):
        # Progress display
        if sim % progress_interval == 0 or sim == num_simulations - 1:
            pct = (sim + 1) / num_simulations * 100
            bar_len = 30
            filled = int(bar_len * (sim + 1) // num_simulations)
            bar = '█' * filled + '░' * (bar_len - filled)
            sys.stdout.write(f"\r  Progress: [{bar}] {pct:5.1f}% ({sim+1:,}/{num_simulations:,})")
            sys.stdout.flush()
        
        # Initialize simulation state
        sim_standings = {
            team: {
                'match_points': standings_before.get(team, {'match_points': 0})['match_points'],
                'game_points': standings_before.get(team, {'game_points': 0.0})['game_points']
            } 
            for team in teams
        }
        sim_matchup_history = historical_matchups.copy()
        sim_color_history = {}  # Will be set after first simulated round
        
        # Get color history from current round pairings
        for matchup in current_matchups:
            if matchup:
                white_team = matchup[0].white_team
                black_team = matchup[0].black_team
                sim_color_history[white_team] = 'white'
                sim_color_history[black_team] = 'black'
        
        # Simulate each remaining round
        for round_num in range(current_round, final_round + 1):
            if round_num == current_round:
                # Use actual pairings for current round
                round_matchups = current_matchups
            else:
                # Generate Swiss pairings for future rounds
                round_matchups = generate_swiss_pairings(
                    sim_standings, sim_matchup_history, sim_color_history,
                    team_rosters, player_ratings
                )
            
            # Simulate each match in this round
            for matchup in round_matchups:
                if not matchup:
                    continue
                
                team_a = matchup[0].white_team
                team_b = matchup[0].black_team
                
                # Determine which we consider "team_a" for scoring (alphabetically)
                if team_a > team_b:
                    team_a, team_b = team_b, team_a
                
                team_a_score = 0.0
                team_b_score = 0.0
                
                for pairing in matchup:
                    if pairing.is_played:
                        # Use actual result
                        game_result = pairing.result
                    else:
                        # Simulate game
                        game_result = simulate_game(pairing.white, pairing.black)
                    
                    # Assign points to correct team
                    if pairing.white_team == team_a:
                        team_a_score += game_result
                        team_b_score += (1 - game_result)
                    else:
                        team_b_score += game_result
                        team_a_score += (1 - game_result)
                
                # Update standings
                if team_a_score > team_b_score:
                    sim_standings[team_a]['match_points'] += 2
                elif team_b_score > team_a_score:
                    sim_standings[team_b]['match_points'] += 2
                else:
                    sim_standings[team_a]['match_points'] += 1
                    sim_standings[team_b]['match_points'] += 1
                
                sim_standings[team_a]['game_points'] += team_a_score
                sim_standings[team_b]['game_points'] += team_b_score
                
                # Record matchup
                matchup_key = tuple(sorted([team_a, team_b]))
                sim_matchup_history.add(matchup_key)
                
                # Update color history (for next round pairing)
                for pairing in matchup:
                    sim_color_history[pairing.white_team] = 'white'
                    sim_color_history[pairing.black_team] = 'black'
                    break  # Only need first board's colors
        
        # Calculate final placements
        final_standings = sorted(
            sim_standings.items(),
            key=lambda x: (-x[1]['match_points'], -x[1]['game_points'], x[0])
        )
        
        for place, (team, stats) in enumerate(final_standings, 1):
            placement_counts[team][place] += 1
            total_placements[team] += place
            total_match_points[team] += stats['match_points']
            total_game_points[team] += stats['game_points']
    
    print("\n")  # New line after progress bar
    
    # Build results
    num_teams = len(teams)
    results = {}
    
    for team in teams:
        probs = {place: count / num_simulations 
                 for place, count in placement_counts[team].items()}
        
        current_stats = standings_before.get(team, {'match_points': 0, 'game_points': 0.0})
        
        results[team] = {
            'placement_probs': probs,
            'expected_placement': total_placements[team] / num_simulations,
            'current_standing': current_placements[team],
            'current_match_points': current_stats['match_points'],
            'current_game_points': current_stats['game_points'],
            'expected_match_points': total_match_points[team] / num_simulations,
            'expected_game_points': total_game_points[team] / num_simulations,
        }
    
    return results


def main():
    parser = argparse.ArgumentParser(description='Predict team match outcomes using Glicko-2 ratings')
    parser.add_argument('--season', '-s', type=int, required=True, help='Season number')
    parser.add_argument('--round', '-r', type=int, required=True, help='Round number')
    parser.add_argument('--output', '-o', type=str, default=None, help='Output prefix (creates _matches.html, _standings.html, _distribution.html)')
    parser.add_argument('--simulations', '-n', type=int, default=10000, help='Number of Monte Carlo simulations')
    parser.add_argument('--no-cache', action='store_true', help='Skip rating cache, fetch fresh from Lichess')
    parser.add_argument('--export-sims', type=str, default=None, help='Export individual simulation results to Excel file')
    parser.add_argument('--full-season', action='store_true', help='Simulate entire remaining season with Swiss pairings')
    
    args = parser.parse_args()
    
    mode = "FULL SEASON" if args.full_season else f"Round {args.round}"
    print(f"\n{'='*60}")
    print(f"MATCH PREDICTOR - Season {args.season} {mode}")
    print(f"{'='*60}\n")
    
    # Fetch season data
    season_data = fetch_season_data(args.season)
    
    # Get all unique players in this round
    games = season_data.get('games', [])
    round_games = [g for g in games if g.get('round') == args.round]
    
    usernames = set()
    for game in round_games:
        usernames.add(game.get('white', '').lower())
        usernames.add(game.get('black', '').lower())
    usernames.discard('')
    
    # Fetch player ratings
    print(f"Loading ratings for {len(usernames)} players...")
    player_ratings = fetch_player_ratings(list(usernames), use_cache=not args.no_cache)
    
    # Get pairings grouped by matchup
    matchups = get_round_pairings(season_data, args.round, player_ratings)
    
    print(f"\nFound {len(matchups)} team matchups")
    
    if args.full_season:
        # FULL SEASON SIMULATION MODE
        print(f"{'='*60}")
        print("FULL SEASON SIMULATION (Swiss pairings for future rounds)")
        print(f"{'='*60}")
        
        standings_prediction = simulate_full_season(
            season_data, args.round, player_ratings, args.simulations
        )
        
        # Sort by expected placement
        sorted_standings = sorted(
            standings_prediction.items(),
            key=lambda x: x[1]['expected_placement']
        )
        
        print(f"{'#':<3} {'Team':<35} {'Now':>5} {'Proj':>5} {'1st':>6} {'Top 3':>7}")
        print("-" * 65)
        for i, (team, stats) in enumerate(sorted_standings, 1):
            top1_prob = stats['placement_probs'].get(1, 0) * 100
            top3_prob = sum(stats['placement_probs'].get(j, 0) for j in range(1, 4)) * 100
            proj = stats['expected_placement']
            print(f"{i:<3} {team[:35]:<35} {stats['current_standing']:>5} {proj:>5.1f} {top1_prob:>5.0f}% {top3_prob:>6.0f}%")
        
        # Generate HTML outputs
        if args.output:
            prefix = args.output.replace('.html', '')
            
            # Standings table (full season projection) - different filename
            standings_html = generate_standings_html(standings_prediction, args.season, args.round)
            # Update title to reflect full season
            standings_html = standings_html.replace(
                f'Season {args.season} Round {args.round}',
                f'Season {args.season} Full Season Projection (from Round {args.round})'
            )
            standings_file = f"{prefix}_season_standings.html"
            with open(standings_file, 'w', encoding='utf-8') as f:
                f.write(standings_html)
            print(f"\n✅ Season standings projection: {standings_file}")
            
            # Distribution heatmap - different filename
            distribution_html = generate_distribution_html(standings_prediction, args.season, args.round)
            distribution_html = distribution_html.replace(
                f'Season {args.season} Round {args.round}',
                f'Season {args.season} Full Season Projection (from Round {args.round})'
            )
            distribution_file = f"{prefix}_season_distribution.html"
            with open(distribution_file, 'w', encoding='utf-8') as f:
                f.write(distribution_html)
            print(f"✅ Season placement distribution: {distribution_file}")
        
        return
    
    # SINGLE ROUND MODE (original behavior)
    print(f"Running {args.simulations:,} simulations...\n")
    
    # Run match predictions
    predictions = []
    for pairings in matchups:
        if not pairings:
            continue
        
        result = simulate_match(pairings, args.simulations)
        predictions.append(result)
        
        # Console output
        team_a = result['team_a']
        team_b = result['team_b']
        a_prob = result['team_a_win_prob'] * 100
        b_prob = result['team_b_win_prob'] * 100
        a_exp = result['team_a_expected']
        b_exp = result['team_b_expected']
        
        print(f"{team_a[:25]:<25} vs {team_b[:25]:<25}")
        print(f"  Win prob: {a_prob:5.1f}% / {b_prob:5.1f}%  |  Expected: {a_exp:.1f} - {b_exp:.1f}")
    
    # Run standings prediction
    print(f"\n{'='*60}")
    print("STANDINGS PREDICTION")
    print(f"{'='*60}\n")
    
    standings_before = get_standings_before_round(season_data, args.round)
    capture_details = args.export_sims is not None
    standings_prediction, detailed_sims = simulate_standings(
        matchups, standings_before, args.simulations, capture_details=capture_details
    )
    
    # Sort by expected placement
    sorted_standings = sorted(
        standings_prediction.items(),
        key=lambda x: x[1]['expected_placement']
    )
    
    print(f"{'#':<3} {'Team':<35} {'Now':>5} {'1st':>6} {'Top 3':>7}")
    print("-" * 60)
    for i, (team, stats) in enumerate(sorted_standings, 1):
        top1_prob = stats['placement_probs'].get(1, 0) * 100
        top3_prob = sum(stats['placement_probs'].get(j, 0) for j in range(1, 4)) * 100
        print(f"{i:<3} {team[:35]:<35} {stats['current_standing']:>5} {top1_prob:>5.0f}% {top3_prob:>6.0f}%")
    
    # Export simulations to Excel if requested
    if args.export_sims and detailed_sims:
        export_simulations_to_excel(detailed_sims, args.export_sims, args.season, args.round)
    
    # Generate HTML outputs
    if args.output:
        prefix = args.output.replace('.html', '')
        
        # 1. Match predictions
        matches_html = generate_matches_html(predictions, args.season, args.round)
        matches_file = f"{prefix}_matches.html"
        with open(matches_file, 'w', encoding='utf-8') as f:
            f.write(matches_html)
        print(f"\n✅ Match predictions: {matches_file}")
        
        # 2. Standings table
        standings_html = generate_standings_html(standings_prediction, args.season, args.round)
        standings_file = f"{prefix}_standings.html"
        with open(standings_file, 'w', encoding='utf-8') as f:
            f.write(standings_html)
        print(f"✅ Standings table: {standings_file}")
        
        # 3. Placement distribution
        distribution_html = generate_distribution_html(standings_prediction, args.season, args.round)
        distribution_file = f"{prefix}_distribution.html"
        with open(distribution_file, 'w', encoding='utf-8') as f:
            f.write(distribution_html)
        print(f"✅ Placement distribution: {distribution_file}")
    
    print(f"\n{'='*60}")
    print("PREDICTION COMPLETE")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
