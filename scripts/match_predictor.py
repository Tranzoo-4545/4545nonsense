#!/usr/bin/env python3
"""
Lichess4545 Match Predictor using Glicko-2 ratings and Monte Carlo simulation.

Predicts team match outcomes based on individual player ratings.
Updates predictions as games complete throughout the week.

Usage:
    python match_predictor.py --season 48 --round 3
    python match_predictor.py --season 48 --round 3 --output predictions.html
    python match_predictor.py --season 48 --round 3 --simulations 50000

The Match Centre (match_centre.html, written next to the other pages: --output predictions -> match_centre.html;
<prefix>_matches.html = predictions_matches.html on the site is only a forwarding page to it, for old links) is drawn by
match_centre.py (next to this script; imported only for that page - without it the old matches page is written)
from scripts/match_centre.json (Monday, match_centre_data.py) + this run's live data. --fetch-calendar (website
only) downloads the league calendar once for the scheduled times.
"""

import argparse
import html as _html
import json
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    double_forfeit: bool = False  # 0F-0F: finished, 0 points to each side (result stays None)

    @property
    def is_played(self) -> bool:
        return self.result is not None or self.double_forfeit


def expected_score(player: Player, opponent: Player) -> float:
    """
    Calculate expected score for player against opponent using Elo formula.
    Returns probability of winning (0 to 1).
    
    Uses simple Elo formula (no RD) to match V6 backtest calibration.
    """
    diff = player.rating - opponent.rating
    return 1 / (1 + 10 ** (-diff / 400))


def calculate_draw_probability(white: Player, black: Player) -> float:
    """
    Calculate draw probability based on ratings.
    
    Calibrated from Lichess4545 seasons 40-48 backtest analysis.
    V6 model - best performing in backtests (Brier: 0.5929, Log Loss: 0.9823).
    """
    avg_rating = (white.rating + black.rating) / 2
    rating_diff = abs(white.rating - black.rating)
    
    # V6 calibrated parameters
    BASE_DRAW_RATE = 0.14
    DRAW_RATING_FACTOR = 0.00018
    DRAW_DIFF_FACTOR = -0.0004
    
    # Draw rate scales with average rating
    draw_prob = BASE_DRAW_RATE + DRAW_RATING_FACTOR * (avg_rating - 1600)
    
    # Larger rating differences reduce draw probability
    draw_prob += DRAW_DIFF_FACTOR * rating_diff
    
    return max(0.02, min(0.25, draw_prob))


def simulate_game(white: Player, black: Player) -> float:
    """
    Simulate a single game outcome.
    Returns 1 (white wins), 0.5 (draw), or 0 (black wins).
    
    Uses Glicko expected score and rating-based draw probability.
    V6 calibrated model from backtest analysis.
    """
    # V6 calibrated parameters
    WHITE_ADVANTAGE = 20  # Elo points for first-move advantage
    MAX_WIN_PROB = 0.85   # Cap confidence to allow upsets
    
    # Create virtual "boosted" white player for expected score calculation
    white_boosted = Player(
        username=white.username,
        rating=white.rating + WHITE_ADVANTAGE,
        rd=white.rd
    )
    
    # Calculate expected score with White's first-move advantage
    white_expected = expected_score(white_boosted, black)
    
    # Calculate draw probability based on actual ratings (not boosted)
    draw_prob = calculate_draw_probability(white, black)
    
    # Distribute remaining probability between win/loss
    remaining = 1 - draw_prob
    white_win_prob = remaining * white_expected
    black_win_prob = remaining * (1 - white_expected)
    
    # Apply confidence cap - redistribute excess to draw and underdog
    if white_win_prob > MAX_WIN_PROB:
        excess = white_win_prob - MAX_WIN_PROB
        white_win_prob = MAX_WIN_PROB
        draw_prob += excess * 0.4
        black_win_prob += excess * 0.6
    elif black_win_prob > MAX_WIN_PROB:
        excess = black_win_prob - MAX_WIN_PROB
        black_win_prob = MAX_WIN_PROB
        draw_prob += excess * 0.4
        white_win_prob += excess * 0.6
    
    # Normalize
    total = white_win_prob + draw_prob + black_win_prob
    white_win_prob /= total
    draw_prob /= total
    black_win_prob /= total
    
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
        if p.double_forfeit:
            continue  # 0-0
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
    Returns (team_a, team_b, team_a_score, team_b_score, team_a_wins, team_b_wins).
    """
    team_a = pairings[0].white_team
    team_b = pairings[0].black_team
    
    team_a_score = 0.0
    team_b_score = 0.0
    team_a_wins = 0
    team_b_wins = 0
    
    for p in pairings:
        if p.double_forfeit:
            continue  # 0-0
        if p.is_played:
            # Use actual result
            if p.white_team == team_a:
                team_a_score += p.result
                team_b_score += (1 - p.result)
                if p.result == 1.0:
                    team_a_wins += 1
                elif p.result == 0.0:
                    team_b_wins += 1
            else:
                team_b_score += p.result
                team_a_score += (1 - p.result)
                if p.result == 1.0:
                    team_b_wins += 1
                elif p.result == 0.0:
                    team_a_wins += 1
        else:
            # Simulate
            game_result = simulate_game(p.white, p.black)
            
            if p.white_team == team_a:
                team_a_score += game_result
                team_b_score += (1 - game_result)
                if game_result == 1.0:
                    team_a_wins += 1
                elif game_result == 0.0:
                    team_b_wins += 1
            else:
                team_b_score += game_result
                team_a_score += (1 - game_result)
                if game_result == 1.0:
                    team_b_wins += 1
                elif game_result == 0.0:
                    team_a_wins += 1
    
    return (team_a, team_b, team_a_score, team_b_score, team_a_wins, team_b_wins)


def get_standings_before_round(season_data: dict, before_round: int) -> dict:
    """
    Calculate standings up to (but not including) a specific round.
    Returns dict: team_name -> {match_points, game_points, games_won, sonneborn_berger, opponents}
    
    Uses same tiebreakers as the league:
    1. Match Points (desc)
    2. Game Points (desc)
    3. Head-to-head score (desc)
    4. Games Won (desc)
    5. Sonneborn-Berger (desc)
    6. Team Name (alphabetical)
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
        'matches': [],  # List of (opponent, match_pts_earned) for SB calculation
        'opponents': defaultdict(float)  # {opponent: game_points_scored} for H2H
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
                elif result in ('1/2-1/2', '1/2Z-1/2Z'):
                    team_a_points += 0.5
                    team_b_points += 0.5
                # 0F-0F (double forfeit): no points for either team
            
            # Update game points, games won, and head-to-head
            teams[team_a]['game_points'] += team_a_points
            teams[team_b]['game_points'] += team_b_points
            teams[team_a]['games_won'] += team_a_wins
            teams[team_b]['games_won'] += team_b_wins
            teams[team_a]['opponents'][team_b] += team_a_points
            teams[team_b]['opponents'][team_a] += team_b_points
            
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
    
    # Convert to simple dict (convert opponents from defaultdict)
    result = {}
    for team_name, team_data in teams.items():
        result[team_name] = {
            'match_points': team_data['match_points'],
            'game_points': team_data['game_points'],
            'games_won': team_data['games_won'],
            'sonneborn_berger': team_data['sonneborn_berger'],
            'opponents': dict(team_data['opponents']),
        }
    
    return result


def sort_standings(items) -> list:
    """
    Sort (team_name, stats_dict) pairs into the league table order - the same order as
    standings.calculate_standings:
    1. Match Points (desc)
    2. Game Points (desc)
    3. Head-to-head (desc), the league's way: within each group of teams level on match points AND
       game points, the match points each team took off the others in that group (works for 3+ team
       ties, unlike comparing two teams at a time). Match points per meeting come from the game points
       in 'opponents' (teams meet at most once a season).
    4. Games Won (desc)
    5. Sonneborn-Berger (desc)
    6. Team Name (alphabetical)
    """
    items = list(items)
    stats_by_team = dict(items)
    groups = defaultdict(set)
    for name, stats in items:
        groups[(stats.get('match_points', 0), stats.get('game_points', 0))].add(name)

    def head_to_head(name, stats):
        group = groups[(stats.get('match_points', 0), stats.get('game_points', 0))]
        scored = stats.get('opponents') or {}
        total = 0
        for opp in group:
            if opp == name or opp not in scored:
                continue
            conceded = (stats_by_team[opp].get('opponents') or {}).get(name, 0)
            total += 2 if scored[opp] > conceded else 1 if scored[opp] == conceded else 0
        return total

    return sorted(items, key=lambda x: (
        -x[1].get('match_points', 0),
        -x[1].get('game_points', 0),
        -head_to_head(*x),
        -x[1].get('games_won', 0),
        -x[1].get('sonneborn_berger', 0),
        x[0],
    ))


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
        # Start with standings before round - include all tiebreaker fields
        sim_standings = {}
        for team in all_teams:
            base = standings_before.get(team, {})
            sim_standings[team] = {
                'match_points': base.get('match_points', 0),
                'game_points': base.get('game_points', 0.0),
                'games_won': base.get('games_won', 0),
                'matches': list(base.get('matches', [])),  # Copy list for SB tracking
                'opponents': dict(base.get('opponents', {}))  # Copy dict for H2H tracking
            }
        
        # Track match results for this simulation
        sim_match_results = [] if capture_details else None
        
        # Simulate each match
        for matchup in all_matchups:
            if not matchup:
                continue
            
            team_a, team_b, score_a, score_b, wins_a, wins_b = simulate_single_match(matchup)
            
            # Update game points, games won, and head-to-head
            sim_standings[team_a]['game_points'] += score_a
            sim_standings[team_b]['game_points'] += score_b
            sim_standings[team_a]['games_won'] += wins_a
            sim_standings[team_b]['games_won'] += wins_b
            sim_standings[team_a]['opponents'][team_b] = sim_standings[team_a]['opponents'].get(team_b, 0) + score_a
            sim_standings[team_b]['opponents'][team_a] = sim_standings[team_b]['opponents'].get(team_a, 0) + score_b
            
            # Update match points and track for SB
            if score_a > score_b:
                sim_standings[team_a]['match_points'] += 2
                sim_standings[team_a]['matches'].append((team_b, 2))
                sim_standings[team_b]['matches'].append((team_a, 0))
            elif score_b > score_a:
                sim_standings[team_b]['match_points'] += 2
                sim_standings[team_a]['matches'].append((team_b, 0))
                sim_standings[team_b]['matches'].append((team_a, 2))
            else:
                sim_standings[team_a]['match_points'] += 1
                sim_standings[team_b]['match_points'] += 1
                sim_standings[team_a]['matches'].append((team_b, 1))
                sim_standings[team_b]['matches'].append((team_a, 1))
            
            if capture_details:
                # Store with consistent team ordering
                if team_a > team_b:
                    sim_match_results.append((score_b, score_a))
                else:
                    sim_match_results.append((score_a, score_b))
        
        # Calculate Sonneborn-Berger for each team
        for team in all_teams:
            sb_score = 0.0
            for opponent, match_pts in sim_standings[team]['matches']:
                opponent_match_pts = sim_standings[opponent]['match_points']
                sb_score += match_pts * opponent_match_pts
            sim_standings[team]['sonneborn_berger'] = sb_score
        
        # Sort to get placements using full tiebreakers (including H2H)
        sorted_teams = sort_standings(sim_standings.items())
        
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
                'standings': {t: {'match_points': s['match_points'], 'game_points': s['game_points']} 
                              for t, s in sim_standings.items()}
            })
    
    # Calculate current standings (before this round's games)
    # Use full tiebreakers including H2H
    default_stats = {'match_points': 0, 'game_points': 0.0, 'games_won': 0, 'sonneborn_berger': 0.0, 'opponents': {}}
    current_sorted = sort_standings([(team, {**default_stats, **standings_before.get(team, {})}) 
         for team in all_teams])
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
    # Double forfeit (0F-0F) is not a single score: caller sets BoardPairing.double_forfeit
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
            double_forfeit=result_str.strip() == '0F-0F',
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


# ---------------------------------------------------------------------------
# HTML PAGES (presentation only - the numbers come from the simulations above)
# ---------------------------------------------------------------------------

# The five prediction pages: key -> (file suffix, nav label). '{r}' = round number.
PREDICTION_PAGES = [
    ('matches', '_matches', 'Match Centre'),
    ('standings', '_standings', 'Table after R{r}'),
    ('distribution', '_distribution', 'Places after R{r}'),
    ('season_distribution', '_season_distribution', 'Season finish'),
]
# predictions_season_standings.html is still written, as a forwarding page to Season finish (old links)
ROUND_PAGE_KEYS = ('matches', 'standings', 'distribution')
SEASON_PAGE_KEYS = ('season_distribution',)


# The Match Centre has a fixed name in the output folder (his call 21 Oct 2026: the site URL is match_centre.html);
# <prefix>_matches.html (predictions_matches.html) is kept as a forwarding page.
CENTRE_FILE = 'match_centre.html'


def centre_path(prefix: str) -> str:
    """match_centre.html in the folder of the output prefix ('predictions' -> 'match_centre.html')."""
    return os.path.join(os.path.dirname(prefix), CENTRE_FILE)


def prediction_nav(prefix: str, round_pages: bool, season_pages: bool) -> dict:
    """Links for the header strip: page key -> href (file name next to the page). Only pages that exist get a link."""
    base = os.path.basename(prefix)
    keys = (ROUND_PAGE_KEYS if round_pages else ()) + (SEASON_PAGE_KEYS if season_pages else ())
    return {key: CENTRE_FILE if key == 'matches' else f'{base}{suffix}.html'
            for key, suffix, _ in PREDICTION_PAGES if key in keys}


def generate_matches_forward_html() -> str:
    """<prefix>_matches.html: forwards to the Match Centre, keeping a #anchor (predictions_matches.html#m5 ->
    match_centre.html#m5)."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Match Centre</title>
    <script>location.replace('{CENTRE_FILE}' + location.hash);</script>
    <meta http-equiv="refresh" content="1; url={CENTRE_FILE}">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f6f6;
            color: #0f2830; margin: 0; padding: 3rem 16px; text-align: center; }}
        a {{ color: #2c5f6f; font-weight: 600; font-size: 1.1rem; }}
    </style>
</head>
<body>
    <p>The Match Centre has moved → <a href="{CENTRE_FILE}">{CENTRE_FILE}</a></p>
</body>
</html>
'''


def fmt_pct(p: float) -> str:
    """A probability (0..1) for display: '–' = never happened (exactly 0), '<1%' / '>99%' = under 1% / over 99% but not 0 / 100."""
    if p <= 0:
        return '–'
    if p >= 1:
        return '100%'
    v = p * 100
    if v < 1:
        return '<1%'
    if v > 99:
        return '>99%'
    return f'{v:.0f}%'


def _esc(text) -> str:
    return _html.escape(str(text), quote=True)


def get_common_css() -> str:
    """Common CSS of all prediction pages (light league teal). Plain string: single braces."""
    return '''
        :root {
            --bg: #f0f6f6;
            --card: #ffffff;
            --card-2: #e7f1f2;
            --ink: #0f2830;
            --muted: #5f7c86;
            --teal: #2c5f6f;
            --teal-2: #3d7a8a;
            --deep: #1a4a5c;
            --pale: #a8d5e2;
            --gold: #d4b856;
            --line: #d3e3e6;
            --team-a: #2c5f6f;
            --team-b: #9a4f2a;
            --draw: #d4b856;
            --mine: #fbf3d6;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--ink);
            line-height: 1.5;
        }
        .topbar {
            background: var(--deep);
            padding: 0.6rem 16px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 0.4rem 1.25rem;
        }
        .topbar a { color: #ffffff; text-decoration: none; }
        .topbar .back { font-weight: 600; white-space: nowrap; }
        .topbar .back:hover { text-decoration: underline; }
        .pages { display: flex; flex-wrap: wrap; gap: 0.35rem; }
        .pages a {
            font-size: 0.85rem;
            padding: 0.2rem 0.65rem;
            border-radius: 999px;
            border: 1px solid rgba(255, 255, 255, 0.35);
            white-space: nowrap;
        }
        .pages a:hover { background: rgba(255, 255, 255, 0.12); }
        .pages a.on { background: #ffffff; color: var(--deep); font-weight: 600; border-color: #ffffff; }
        .page { max-width: 1200px; margin: 0 auto; padding: 1.5rem 16px 3rem; }
        h1 {
            text-align: center;
            color: var(--teal);
            font-size: 1.8rem;
            line-height: 1.25;
            margin-bottom: 0.4rem;
        }
        h2 { color: var(--teal); font-size: 1.2rem; margin: 2.5rem 0 0.75rem; }
        .subtitle { text-align: center; color: var(--muted); margin-bottom: 1.5rem; }
        .note { color: var(--muted); font-size: 0.8rem; margin: 0.6rem 0 0; }
        .picker {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            margin: 0 auto 1.5rem;
        }
        .picker label { color: var(--muted); font-size: 0.9rem; }
        .picker select {
            font: inherit;
            font-size: 0.9rem;
            max-width: 100%;
            padding: 0.35rem 0.5rem;
            border: 1px solid var(--pale);
            border-radius: 8px;
            background: var(--card);
            color: var(--ink);
        }
        .picker button {
            font: inherit;
            font-size: 0.85rem;
            padding: 0.35rem 0.8rem;
            border: 1px solid var(--teal);
            border-radius: 8px;
            background: var(--card);
            color: var(--teal);
            cursor: pointer;
        }
        .picker button[hidden] { display: none; }
        .box {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            overflow-x: auto;
        }
        table.std { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        table.std th {
            background: var(--card-2);
            color: var(--muted);
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            padding: 0.6rem 0.4rem;
            text-align: center;
        }
        table.std td { padding: 0.5rem 0.4rem; text-align: center; border-top: 1px solid var(--line); }
        table.std th.tl, table.std td.tl { text-align: left; padding-left: 0.75rem; }
        td.team { font-weight: 600; overflow-wrap: anywhere; min-width: 7rem; }
        td.rank { color: var(--teal); font-weight: 700; width: 2.2rem; }
        td.dim { color: var(--muted); }
        td.lbl { font-size: 0.72rem; font-weight: 700; line-height: 1.2; }
        td.lbl.yes { color: var(--teal); }
        td.lbl.no { color: var(--muted); font-weight: 500; }
        tr.mine td:not([style]), .team-card.mine { background: var(--mine); }
        tr.mine td.team { color: var(--deep); }
        tr.mine td:first-child { box-shadow: inset 4px 0 0 var(--gold); }
        .team-card.mine { border-color: var(--gold); box-shadow: inset 4px 0 0 var(--gold); }
        @media (max-width: 600px) {
            h1 { font-size: 1.4rem; }
            table.std { font-size: 0.8rem; }
            table.std th { font-size: 0.62rem; letter-spacing: 0; padding: 0.5rem 0.2rem; }
            table.std td { padding: 0.45rem 0.2rem; }
            table.std th.tl, table.std td.tl { padding-left: 0.5rem; }
            td.team { min-width: 6rem; }
        }
'''


PICKER_JS = '''
<script>
(function () {
    var KEY = '4545-predictions-my-team';
    var sel = document.getElementById('team-pick');
    var clear = document.getElementById('team-clear');
    if (!sel) return;
    function apply(team) {
        var lists = document.querySelectorAll('[data-pin-list]');
        for (var l = 0; l < lists.length; l++) {
            var list = lists[l];
            var items = Array.prototype.slice.call(list.children).filter(function (e) { return e.hasAttribute('data-i'); });
            items.sort(function (a, b) { return a.getAttribute('data-i') - b.getAttribute('data-i'); });
            items.forEach(function (e) { e.classList.remove('mine'); list.appendChild(e); });
            items.forEach(function (e) {
                if (team && e.getAttribute('data-team') === team) {
                    e.classList.add('mine');
                    list.insertBefore(e, list.firstChild);
                }
            });
        }
        var marks = document.querySelectorAll('[data-mark]');
        for (var m = 0; m < marks.length; m++) {
            marks[m].classList.toggle('mine', !!team && marks[m].getAttribute('data-team') === team);
        }
        clear.hidden = !team;
    }
    function remember(team) {
        try {
            if (team) { localStorage.setItem(KEY, team); } else { localStorage.removeItem(KEY); }
        } catch (e) {}
    }
    sel.addEventListener('change', function () { remember(sel.value); apply(sel.value); });
    clear.addEventListener('click', function () { sel.value = ''; remember(''); apply(''); });
    var saved = '';
    try { saved = localStorage.getItem(KEY) || ''; } catch (e) {}
    var known = Array.prototype.some.call(sel.options, function (o) { return o.value === saved; });
    if (saved && known) { sel.value = saved; apply(saved); }
})();
</script>
'''


def _picker_html(teams) -> str:
    """'Your team' dropdown (pins + highlights the team's rows/cards; remembered on this device)."""
    options = ''.join(f'<option value="{_esc(t)}">{_esc(t)}</option>' for t in sorted(teams, key=str.lower))
    return f'''
    <div class="picker">
        <label for="team-pick">Your team:</label>
        <select id="team-pick"><option value="">Choose a team…</option>{options}</select>
        <button type="button" id="team-clear" hidden>Show all</button>
    </div>
'''


def _page(title: str, h1: str, subtitle: str, body: str, current: str, round_num: int,
          nav: Optional[dict], extra_css: str = '', script: str = '') -> str:
    """Page shell: header strip (back link + links between the prediction pages), title, body."""
    if nav is None:
        nav = {current: ''}
    links = ''
    for key, _, label in PREDICTION_PAGES:
        if key not in nav:
            continue
        text = _esc(label.format(r=round_num))
        if key == current:
            links += f'<a class="on" aria-current="page" href="{_esc(nav[key] or "#")}">{text}</a>'
        else:
            links += f'<a href="{_esc(nav[key])}">{text}</a>'
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_esc(title)}</title>
    <style>
        {get_common_css()}
        {extra_css}
    </style>
</head>
<body>
    <nav class="topbar">
        <a class="back" href="index.html">← The 4545 Chronicles</a>
        <div class="pages">{links}</div>
    </nav>
    <main class="page">
    <h1>{h1}</h1>
    <p class="subtitle">{subtitle}</p>
{body}
    </main>
{script}
</body>
</html>
'''


def _updated() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _mode_texts(mode: str, season: int, round_num: int, n_sims: Optional[int]) -> dict:
    """Titles per page kind. mode: 'round' (this round), 'season' (rest of season), 'preseason' (from rosters)."""
    sims = f'{n_sims:,} simulations' if n_sims else 'Monte Carlo simulation'
    if mode == 'round':
        return {
            'standings_title': f'Table after Round {round_num} - Season {season}',
            'standings_h1': f'Table after Round {round_num}',
            'standings_sub': f'Season {season} · projected from {sims} of this round · updated {_updated()}',
            'dist_title': f'Places after Round {round_num} - Season {season}',
            'dist_h1': f'Places after Round {round_num}',
            'dist_sub': f'Season {season} · chance of each place once Round {round_num} is over · {sims} · updated {_updated()}',
            'first': '1st',
        }
    when = 'pre-season, from the rosters' if mode == 'preseason' else f'from Round {round_num}'
    return {
        'standings_title': f'Season finish: projected table - Season {season}',
        'standings_h1': 'Season finish: projected table',
        'standings_sub': f'Season {season} · {when} · {sims} of the rest of the season · updated {_updated()}',
        'dist_title': f'Season finish - Season {season}',
        'dist_h1': 'Season finish',
        'dist_sub': f'Season {season} · {when} · {sims} of the rest of the season · updated {_updated()}',
        'first': 'Title',
    }


def _sorted_standings(standings_prediction: dict) -> list:
    return sorted(standings_prediction.items(), key=lambda x: x[1]['expected_placement'])


def _settled_by_points(standings_prediction: dict, team: str, top_k: int, rounds_left: Optional[int]) -> Optional[str]:
    """'yes' = a top-k finish is certain from match points alone, 'no' = impossible from match points alone,
    None = open (or not checkable). Conservative: a team that could still TIE on match points counts as a threat,
    tiebreaks are never used, and the simulations must agree (100% / never)."""
    if not rounds_left or rounds_left < 0:
        return None
    max_gain = 2 * rounds_left
    me = standings_prediction[team].get('current_match_points', 0)
    others = [s.get('current_match_points', 0) for t, s in standings_prediction.items() if t != team]
    sim_p = sum(standings_prediction[team]['placement_probs'].get(j, 0) for j in range(1, top_k + 1))
    could_reach_me = sum(1 for mp in others if mp + max_gain >= me)       # may finish level or above
    surely_above = sum(1 for mp in others if mp > me + max_gain)          # ahead whatever happens
    if 1 + could_reach_me <= top_k and sim_p >= 1:
        return 'yes'
    if 1 + surely_above > top_k and sim_p <= 0:
        return 'no'
    return None


def _prob_cell(p: float, settled: Optional[str] = None) -> str:
    if settled == 'yes':
        return '<td class="lbl yes">Clinched</td>'
    if settled == 'no':
        return '<td class="lbl no">Out of reach</td>'
    if p <= 0:
        return f'<td class="dim">{fmt_pct(p)}</td>'
    if p < 0.5:      # pale teal, stronger with the chance
        style = f'background: rgba(168, 213, 226, {0.15 + 1.7 * p:.2f});'
    else:            # solid league teal (#3d7a8a at 50% -> #1a4a5c at 100%), white text
        f = (p - 0.5) * 2
        rgb = tuple(round(a + (b - a) * f) for a, b in zip((61, 122, 138), (26, 74, 92)))
        style = f'background: rgb{rgb}; color: #fff; font-weight: 600;'
    return f'<td style="{style}">{fmt_pct(p)}</td>'


# ---------------------------------------------------------------------------
# WHAT'S AT STAKE (from the --full-season simulations, split by this round's result of each match)
# ---------------------------------------------------------------------------

# A Win / Draw / Loss figure needs at least this many simulated seasons with that result, else it shows '–'
MIN_BRANCH_SIMS = 1000
# A team's title / top-3 chance must move by at least this much between a win and a loss to be shown ("At stake" line)
STAKE_SWING_MIN = 0.10
STAKE_MEASURES = (('title', 'Title'), ('top3', 'Top 3'))


def match_stakes(stakes: dict) -> list:
    """Per match of the round, from simulate_full_season(stakes_out=...): for each team and measure the chance at
    the end of the season after a win / draw / loss in this match (None = fewer than MIN_BRANCH_SIMS seasons)
    and the swing = after a win minus after a loss (when one of them is missing: the gap between the results
    that are left; 0 when fewer than two are left, e.g. a match already decided)."""
    out = []
    for m in stakes.get('matches', []):
        a, b, n = m['a'], m['b'], m['n']
        teams = {}
        for team, order in ((a, (0, 1, 2)), (b, (2, 1, 0))):     # the team's win / draw / loss
            info = {}
            for key, _ in STAKE_MEASURES:
                hits = m[key][team]
                wdl = tuple(hits[o] / n[o] if n[o] >= MIN_BRANCH_SIMS else None for o in order)
                w, _, l = wdl
                if w is not None and l is not None:
                    swing = w - l
                else:
                    known = [v for v in wdl if v is not None]
                    swing = max(known) - min(known) if len(known) >= 2 else 0.0
                info[key] = {'wdl': wdl, 'swing': swing,
                             'now': sum(hits) / sum(n) if sum(n) else None}   # all results together = the chance today
            teams[team] = info
        out.append({'a': a, 'b': b, 'teams': teams})
    return out


def _table_key(team_a: str, team_b: str, places: dict) -> tuple:
    """Order of matches by table position (Swiss 'top boards'): the better place of the two teams, then the other."""
    pa, pb = places.get(team_a, 999), places.get(team_b, 999)
    return (min(pa, pb), max(pa, pb), team_a.lower())


def featured_stake(stake_list: list, places: dict) -> Optional[dict]:
    """The match with the biggest swing: by title swing while the title race is open (some team's title chance
    moves STAKE_SWING_MIN+), else by the biggest title / top-3 swing. None when no match reaches STAKE_SWING_MIN.
    Ties: the match of the higher-placed teams."""
    if not stake_list:
        return None

    def best(m, keys):
        return max(m['teams'][t][k]['swing'] for t in (m['a'], m['b']) for k in keys)
    keys = ('title',) if max(best(m, ('title',)) for m in stake_list) >= STAKE_SWING_MIN else ('title', 'top3')
    ranked = sorted(stake_list, key=lambda m: (-best(m, keys), _table_key(m['a'], m['b'], places)))
    return ranked[0] if best(ranked[0], keys) >= STAKE_SWING_MIN else None


def _stake_pct(p: Optional[float]) -> str:
    return '–' if p is None else _esc(fmt_pct(p))


def _stake_wdl(wdl: tuple, words: bool, now: Optional[float] = None, after_now: str = '') -> str:
    """'Win 54% · Draw 32% · Loss 14%' (words=True) or '54% · 32% · 14%', numbers bold; with now: 'Now 40% | Win …'
    (after_now: e.g. the this-week arrow, right after the Now figure)."""
    lead = (f'<span class="nw"><span class="k">Now</span> <b>{_stake_pct(now)}</b>{after_now}</span><span class="bar">|</span>'
            if now is not None else '')
    parts = []
    for word, p in zip(('Win', 'Draw', 'Loss'), wdl):
        num = f'<b>{_stake_pct(p)}</b>'
        parts.append(f'<span class="nw"><span class="k">{word}</span> {num}</span>' if words else num)
    return lead + '<span class="sep">·</span>'.join(parts)


def _stake_has_dash(m: dict, teams=None, keys=None) -> bool:
    """True when a figure of this block shows '–' (too few seasons, or never happened)."""
    for t in (teams or (m['a'], m['b'])):
        for k in (keys or [k for k, _ in STAKE_MEASURES]):
            if any(p is None or p <= 0 for p in m['teams'][t][k]['wdl']):
                return True
    return False


STAKE_DASH_NOTE = f' – = fewer than {MIN_BRANCH_SIMS:,} simulated seasons had that result, or it never happened.'

STAKE_CSS = '''
        .stake-feat .sep, .stake-line .sep, .stake-card .sep { color: var(--pale); margin: 0 0.3rem; }
        .stake-feat .k, .stake-line .k, .stake-card .k { color: var(--muted); font-size: 0.75rem; font-weight: 400; }
        .stake-feat .bar, .stake-card .bar { color: var(--pale); margin: 0 0.5rem; }
        .nw { white-space: nowrap; }
        .s-lab { color: var(--muted); font-size: 0.72rem; }
        .t-a { color: var(--team-a); }
        .t-b { color: var(--team-b); }
        .fs-tag { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--teal); font-weight: 700;
            margin-bottom: 0.4rem; }
        .fs-line { display: flex; align-items: baseline; gap: 0.4rem; font-size: 0.88rem; font-variant-numeric: tabular-nums; }
        .fs-line .s-lab { width: 2.9rem; flex: none; }
        .fs-note { color: var(--muted); font-size: 0.75rem; margin-top: 0.5rem; }
'''

STAKE_MATCHES_CSS = STAKE_CSS + '''
        .match-card.featured { border: 2px solid var(--teal); scroll-margin-top: 0.75rem; }
        .stake-feat { margin: 1rem 1.25rem 0; padding: 0.7rem 0.9rem; background: var(--bg); border-radius: 8px; }
        .fs-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem 1.25rem; }
        .fs-team { min-width: 0; display: flex; flex-direction: column; }
        .fs-stats { margin-top: auto; }
        .fs-name { font-weight: 700; font-size: 0.85rem; overflow-wrap: anywhere; line-height: 1.3; margin-bottom: 0.15rem; }
        .stake-line { margin: 0.9rem 1.25rem 0; padding: 0.45rem 0.75rem; border-left: 3px solid var(--pale); font-size: 0.82rem; }
        .st-head { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--teal); font-weight: 700; }
        .st-head .k { text-transform: none; letter-spacing: 0; font-size: 0.72rem; }
        .st-line { display: flex; flex-wrap: wrap; gap: 0 1.25rem; font-variant-numeric: tabular-nums; }
        .st-name { font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
        .st-nums { white-space: nowrap; }
        .st-gap { display: inline-block; width: 0.9rem; }
        .st-note { color: var(--muted); font-size: 0.72rem; }
        @media (max-width: 768px) {
            .stake-feat, .stake-line { margin-left: 0.75rem; margin-right: 0.75rem; }
            .fs-grid { grid-template-columns: 1fr; }
        }
'''

STAKE_SEASON_CSS = STAKE_CSS + '''
        .stake-card { max-width: 860px; margin: 0 auto 1.5rem; background: var(--card); border: 1px solid var(--line);
            border-top: 4px solid var(--teal); border-radius: 12px; padding: 0.75rem 0.75rem 0.7rem; }
        .stake-card .fs-tag { margin-left: 0.5rem; }
        .fx-teams { display: grid; grid-template-columns: 1fr auto 1fr; gap: 0.5rem 0.75rem; align-items: stretch; }
        .fx-vs { color: var(--muted); font-size: 0.8rem; align-self: center; }
        .fx-team { min-width: 0; border-radius: 8px; padding: 0.35rem 0.5rem; display: flex; flex-direction: column; }
        .fx-stats { margin-top: auto; }
        .fx-team.mine { background: var(--mine); box-shadow: inset 4px 0 0 var(--gold); }
        .fx-name { font-weight: 700; color: var(--deep); margin-bottom: 0.3rem; line-height: 1.3; overflow-wrap: anywhere; }
        .fx-now { color: var(--muted); font-size: 0.72rem; font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }
        .fx-foot { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 0.25rem 1rem;
            margin: 0.35rem 0.5rem 0; }
        .fx-foot .fs-note { margin: 0; }
        .fx-foot a { color: var(--teal); font-weight: 600; font-size: 0.85rem; white-space: nowrap; text-decoration: none; }
        .fx-foot a:hover { text-decoration: underline; }
        @media (max-width: 600px) {
            .fx-teams { grid-template-columns: 1fr; gap: 0.2rem; }
            .fx-vs { align-self: start; padding-left: 0.5rem; }
            .fx-team { padding: 0.35rem 0.4rem; }
        }
'''


def _stake_feature_block(m: dict, team_a: str, team_b: str) -> str:
    """Matches page: the full panel inside the featured match card (Title / Top 3 : Win · Draw · Loss, both teams)."""
    rows = ''
    for team, side in ((team_a, 'a'), (team_b, 'b')):
        lines = ''.join(f'<div class="fs-line"><span class="s-lab">{label}</span>'
                        f'<span>{_stake_wdl(m["teams"][team][key]["wdl"], True, m["teams"][team][key]["now"])}</span></div>'
                        for key, label in STAKE_MEASURES)
        rows += f'<div class="fs-team"><div class="fs-name t-{side}">{_esc(team)}</div><div class="fs-stats">{lines}</div></div>'
    dash = STAKE_DASH_NOTE if _stake_has_dash(m) else ''
    return (f'<div class="stake-feat"><div class="fs-tag">Biggest potential swing this round · what\'s at stake</div>'
            f'<div class="fs-grid">{rows}</div>'
            f'<p class="fs-note">Chance at the end of the season after a win · draw · loss in this match, from the season '
            f'simulations. Boards already played count as played, so the numbers move during the week (updated 4× a day).'
            f'{dash}</p></div>')


def _stake_line_block(m: dict, team_a: str, team_b: str) -> str:
    """Matches page, other matches: only the teams / measures moving STAKE_SWING_MIN+ between a win and a loss."""
    lines = ''
    dash = False
    for team, side in ((team_a, 'a'), (team_b, 'b')):
        keys = [(k, label) for k, label in STAKE_MEASURES if m['teams'][team][k]['swing'] >= STAKE_SWING_MIN]
        if not keys:
            continue
        dash = dash or _stake_has_dash(m, [team], [k for k, _ in keys])
        parts = '<span class="st-gap"></span>'.join(
            f'<span class="s-lab">{label}</span> {_stake_wdl(m["teams"][team][k]["wdl"], False)}' for k, label in keys)
        lines += (f'<div class="st-line"><span class="st-name t-{side}">{_esc(team)}</span>'
                  f'<span class="st-nums">{parts}</span></div>')
    if not lines:
        return ''
    note = f'<div class="st-note">{STAKE_DASH_NOTE.strip()}</div>' if dash else ''
    return (f'<div class="stake-line"><div class="st-head">At stake <span class="k">(season chance after win · draw · loss)'
            f'</span></div>{lines}{note}</div>')


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def stake_season_card(m: dict, places: dict, round_num: int, matches_href: Optional[str],
                      week_ref: Optional[dict] = None) -> str:
    """Season finish: the featured match as one compact card (higher-placed team first).
    week_ref: the round's reference (prediction history) - this-week arrows after the Now figures."""
    parts = []
    for team in sorted((m['a'], m['b']), key=lambda t: places.get(t, 999)):
        lines = ''
        for key, label in STAKE_MEASURES:
            info = m['teams'][team][key]
            lines += (f'<div class="fs-line"><span class="s-lab">{label}</span>'
                      f'<span>{_stake_wdl(info["wdl"], True, info["now"], _week_arrow(info["now"], week_ref, team, key))}'
                      f'</span></div>')
        now = f'<div class="fx-now">Now {_ordinal(places[team])}</div>' if team in places else ''
        parts.append(f'<div class="fx-team" data-team="{_esc(team)}" data-mark>{now}<div class="fx-name">'
                     f'{_esc(team)}</div><div class="fx-stats">{lines}</div></div>')
    link = f'<a href="{_esc(matches_href)}#stakes">All matches this round →</a>' if matches_href else ''
    dash = STAKE_DASH_NOTE if _stake_has_dash(m) else ''
    return f'''
    <section class="stake-card" aria-label="Biggest potential swing this round">
        <div class="fs-tag">Biggest potential swing this round</div>
        <div class="fx-teams">{parts[0]}<div class="fx-vs">vs</div>{parts[1]}</div>
        <div class="fx-foot"><p class="fs-note">Season chance after a win · draw · loss in this Round {round_num} match.{dash}</p>{link}</div>
    </section>
'''


# ---------------------------------------------------------------------------
# PREDICTION HISTORY ("this week" arrows + "By round" charts on Season finish)
# ---------------------------------------------------------------------------
# prediction_history.json next to this script (v7 locally, the website repo's scripts/ in GitHub Actions):
# {"seasons": {"50": {"5": {"source": "reference", "teams": {team: {"title": 0.39, "top3": ..., "top8": ...,
#   "top15": ..., "place": 1}}, ...}}}}. Per round ONE entry, never overwritten:
#   "reference" = the --full-season projection with that round's boards treated as unplayed (the state when the
#                 round started on Monday), computed by the first --full-season run of the round that finds none;
#   "published" = backfilled once from the website's history (first published run of the round; whole percents).
HISTORY_FILE = Path(__file__).resolve().parent / 'prediction_history.json'
HISTORY_SEASONS_KEPT = 2        # the current season + the previous one
WEEK_ARROW_MIN = 0.03           # a this-week arrow from 3 points of change
WEEK_ARROW_BIG = 0.10           # 10+ points: gold pill
HISTORY_KEYS = (('title', 1), ('top3', 3), ('top8', 8), ('top15', 15))


def load_prediction_history(path: Optional[Path] = None) -> Optional[dict]:
    """The history file; {} when there is none yet, None when it can't be read (then nothing is shown or written)."""
    path = Path(path or HISTORY_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get('seasons', {}), dict):
            raise ValueError('unexpected layout')
        return data
    except Exception as e:
        print(f"⚠ Prediction history unreadable ({path.name}: {e}) - no this-week arrows / charts, file left as it is")
        return None


def save_prediction_history(data: dict, season: int, path: Optional[Path] = None) -> bool:
    """Write the history (only the current season + the previous one kept). Failure = a warning, never an error."""
    path = Path(path or HISTORY_FILE)
    try:
        seasons = data.setdefault('seasons', {})
        for key in list(seasons):
            if not key.isdigit() or int(key) <= season - HISTORY_SEASONS_KEPT:
                del seasons[key]
        tmp = path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        tmp.replace(path)
        return True
    except Exception as e:
        print(f"⚠ Could not save the prediction history ({e})")
        return False


def projection_summary(standings_prediction: dict) -> dict:
    """team -> {title, top3, top8, top15, place now} (fractions, 4 decimals) of a season projection."""
    out = {}
    for team, stats in standings_prediction.items():
        probs = stats['placement_probs']
        row = {key: round(sum(probs.get(j, 0) for j in range(1, k + 1)), 4) for key, k in HISTORY_KEYS}
        row['place'] = stats.get('current_standing')
        out[team] = row
    return out


def season_reference_data(season_data: dict, round_num: int) -> dict:
    """The season with this round's boards treated as unplayed (previous results + this round's pairings)."""
    games = []
    for g in season_data.get('games', []):
        if g.get('round') == round_num:
            g = dict(g)
            g['result'] = ''
        games.append(g)
    return {**season_data, 'games': games}


def update_prediction_history(season: int, round_num: int, season_data: dict, matchups: list, player_ratings: dict,
                              standings_prediction: dict, n_sims: int, path: Optional[Path] = None,
                              stakes: Optional[list] = None, places: Optional[dict] = None) -> Optional[dict]:
    """This season's history as {round: entry}; adds this round's reference first if there is none yet (never
    overwrites one). The reference simulation uses its own seeded random numbers and restores the shared state
    afterwards, so the page's numbers don't depend on whether a reference was made in this run.
    With stakes (match_stakes() of this run) the reference also keeps the round's 'featured' match - the Biggest swing
    as the round started (his call 23 Oct 2026: it must not travel during the week as results come in); a reference
    saved before that gets it added once, from the same start-of-round simulation.
    None = history unreadable (no arrows / charts)."""
    history = load_prediction_history(path)
    if history is None:
        return None
    rounds = history.setdefault('seasons', {}).setdefault(str(season), {})
    entry = rounds.get(str(round_num))
    if entry is None or (stakes is not None and 'featured' not in entry):
        played = sum(1 for m in matchups for p in m if p.is_played)
        ref_stakes = stakes
        if played == 0:
            ref = standings_prediction          # nothing played yet: this projection is the round's starting point
        else:
            print(f"Reference for Round {round_num} (its {played} played boards treated as unplayed)...")
            state = random.getstate()
            random.seed(f'4545 reference S{season} R{round_num}')
            raw = {}
            try:
                ref = simulate_full_season(season_reference_data(season_data, round_num), round_num,
                                           player_ratings, n_sims, stakes_out=raw)
            finally:
                random.setstate(state)
            ref_stakes = match_stakes(raw) if stakes is not None else None
        if entry is None:
            entry = rounds[str(round_num)] = {
                'source': 'reference',
                'created': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                'n_sims': n_sims,
                'boards_played_then': played,
                'teams': projection_summary(ref),
            }
        if stakes is not None:
            feat = featured_stake(ref_stakes, places or {}) if ref_stakes else None
            entry['featured'] = json.loads(json.dumps(feat)) if feat else None     # None = no match reached the bar
            print(f"Biggest swing this round (as it started): {feat['a']} vs {feat['b']}" if feat else
                  "Biggest swing this round (as it started): none")
        if not _ratings_look_fetched(matchups):
            print("⚠ Most ratings missing (fetch failed?) - this round's reference not saved")
        elif save_prediction_history(history, season, path):
            print(f"✅ Prediction history: Round {round_num} reference saved ({Path(path or HISTORY_FILE).name})")
    return {int(r): e for r, e in rounds.items() if r.isdigit() and isinstance(e, dict)}


def _ratings_look_fetched(matchups: list) -> bool:
    """False when most of the round's players have the default rating (the ratings fetch failed) -
    a reference made from those would be wrong for the whole week, so it is not saved."""
    players = [pl for m in matchups for p in m for pl in (p.white, p.black)]
    if not players:
        return False
    default = sum(1 for pl in players if pl.rating == DEFAULT_RATING and pl.rd == DEFAULT_RD)
    return default <= len(players) / 2


def _week_arrow(now: Optional[float], ref: Optional[dict], team: str, key: str) -> str:
    """'▲5' / '▼12' (gold pill from WEEK_ARROW_BIG) = change since the round's reference; '' under WEEK_ARROW_MIN."""
    if now is None or not ref or team not in ref or ref[team].get(key) is None:
        return ''
    delta = now - ref[team][key]
    if abs(delta) < WEEK_ARROW_MIN:
        return ''
    n = round(abs(delta) * 100)
    sym, word = ('▲', 'up') if delta > 0 else ('▼', 'down')
    cls = 'wk big' if abs(delta) >= WEEK_ARROW_BIG else 'wk'
    return f'<span class="{cls}" title="{word} {n} points this week">{sym}{n}</span>'


def _pct_label(v: float) -> str:
    return '<1%' if 0 < v < 0.005 else f'{round(v * 100)}%'


def _trend_svg(team: str, points: list) -> str:
    """Small line chart, the same 0-100% scale for every team: title (solid), top 3 (dashed), top 15 (dotted) per round
    + Now. points: [(label, title, top3, top15)], None = no data (top15 may be None alone for older entries)."""
    ymax = 100                     # the same scale for every team (his call: per-team scales were misleading)
    x0, x1, y0, y1 = 34, 252, 10, 70
    step = (x1 - x0) / max(1, len(points) - 1)
    xs = [x0 + i * step for i in range(len(points))]

    def y(v):
        return y1 - (v * 100 / ymax) * (y1 - y0)
    out = [f'<svg viewBox="0 0 330 94" class="trend-svg" role="img" '
           f'aria-label="Title, top 3 and top 15 chance by round for {_esc(team)}">',
           f'<line x1="{x0}" x2="{x1}" y1="{y1}" y2="{y1}" stroke="#d3e3e6" stroke-width="1"/>',
           *(f'<line x1="{x0}" x2="{x1}" y1="{y(g / 100):.1f}" y2="{y(g / 100):.1f}" stroke="#e7f1f2" stroke-width="1" '
             f'stroke-dasharray="2 3"/>' for g in (25, 50, 75, 100)),
           f'<text x="{x0 - 6}" y="{y1 + 3.5}" text-anchor="end" font-size="11" fill="#5f7c86">0</text>',
           f'<text x="{x0 - 6}" y="{y(0.5) + 3.5:.1f}" text-anchor="end" font-size="11" fill="#5f7c86">50%</text>',
           f'<text x="{x0 - 6}" y="{y0 + 3.5}" text-anchor="end" font-size="11" fill="#5f7c86">100%</text>']
    for i, p in enumerate(points):
        bold = ' font-weight="700"' if p[0] == 'Now' else ''
        out.append(f'<text x="{xs[i]:.1f}" y="88" text-anchor="middle" font-size="11" fill="#5f7c86"{bold}>{p[0]}</text>')
    ends = {}
    #                 index, colour,    dash,    dot radius - drawn widest band first, title on top
    for idx, color, dash, rad in ((3, '#8fbfcc', '1.5 3', 2.8), (2, '#5f9aaa', '4 3', 3.2), (1, '#1a4a5c', '', 3.6)):
        seg = [(xs[i], y(p[idx])) for i, p in enumerate(points) if p[idx] is not None]
        if not seg:
            continue
        d = ' '.join(('M' if j == 0 else 'L') + f'{a:.1f},{b:.1f}' for j, (a, b) in enumerate(seg))
        da = f' stroke-dasharray="{dash}"' if dash else ''
        if len(seg) > 1:
            out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" '
                       f'stroke-linecap="round"{da}/>')
        for a, b in seg:
            out.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="{rad}" fill="{color}" stroke="#ffffff" stroke-width="1.5"/>')
        ends[idx] = seg[-1][1]
    # direct labels at the right end, top to bottom, at least 12 apart (pushed down, then back up if past the bottom)
    names = {1: 'Title', 2: 'Top 3', 3: 'Top 15'}
    now = points[-1]
    order = sorted(ends, key=lambda k: (ends[k], -k))           # ties: Top 15 above Top 3 above Title, like the lines
    ys = []
    for k in order:
        ys.append(max(ends[k], ys[-1] + 12) if ys else ends[k])
    over = ys[-1] - (y1 + 4) if ys else 0
    if over > 0:
        ys = [v - over for v in ys]
    for k, ly in zip(order, ys):
        out.append(f'<text x="{x1 + 9}" y="{ly + 3.5:.1f}" font-size="11.5" fill="#0f2830">{names[k]} '
                   f'<tspan font-weight="700">{_esc(_pct_label(now[k]))}</tspan></text>')
    for i, p in enumerate(points):                 # hover: one column per point with a tooltip
        if p[1] is None:
            tip = f'{p[0]}: no data'
        else:
            when = 'Now' if p[0] == 'Now' else f'{p[0]} start'
            t15 = f' · Top 15 {_pct_label(p[3])}' if p[3] is not None else ''
            tip = f'{when}: Title {_pct_label(p[1])} · Top 3 {_pct_label(p[2])}{t15}'
        out.append(f'<rect x="{xs[i] - step / 2:.1f}" y="2" width="{step:.1f}" height="90" fill="transparent">'
                   f'<title>{_esc(tip)}</title></rect>')
    out.append('</svg>')
    return ''.join(out)


def _trend_block(team: str, history_rounds: dict, round_num: int, now: dict) -> str:
    """'By round' chart of one team: each round's start (history) + Now. '' when the history has nothing."""
    points = []
    for r in range(1, round_num + 1):
        row = history_rounds.get(r, {}).get('teams', {}).get(team)
        points.append((f'R{r}', row['title'], row['top3'], row.get('top15')) if row else (f'R{r}', None, None, None))
    points.append(('Now', now['title'], now['top3'], now.get('top15')))
    return ('<div class="trend"><div class="trend-head"><span>By round</span>'
            '<span class="trend-key"><i class="sw sw-t"></i>Title <i class="sw sw-3"></i>Top 3 <i class="sw sw-15"></i>Top 15</span></div>'
            + _trend_svg(team, points) + '</div>')


WEEK_CSS = '''
        .wk { display: block; font-size: 0.68rem; font-weight: 600; line-height: 1.1; margin-top: 0.1rem; opacity: 0.85;
            font-variant-numeric: tabular-nums; white-space: nowrap; }
        .wk.big { display: table; margin: 0.1rem auto 0; opacity: 1; background: #d4b856; color: #0f2830; border-radius: 999px;
            padding: 0.05rem 0.35rem; font-weight: 700; }
        .fs-line .wk { display: inline-block; margin: 0 0 0 0.3rem; }
        .wk-key { color: var(--muted); font-size: 0.8rem; margin: 0 0 0.5rem; }
        .wk-key .wk { display: inline-block; color: var(--ink); }
        .trend { margin-top: 0.6rem; border-top: 1px solid var(--line); padding-top: 0.5rem; }
        .trend-head { display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem;
            font-size: 0.75rem; color: var(--muted); }
        .trend-head > span:first-child { text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; color: var(--teal); }
        .trend-key { white-space: nowrap; }
        .sw { display: inline-block; width: 16px; height: 0; vertical-align: middle; margin: 0 0.25rem 0 0.5rem; }
        .sw-t { border-top: 2px solid #1a4a5c; }
        .sw-3 { border-top: 2px dashed #5f9aaa; }
        .sw-15 { border-top: 2px dotted #8fbfcc; }
        .trend-svg { width: 100%; height: auto; display: block; margin-top: 0.2rem; }
        .trend-none { color: var(--muted); font-size: 0.8rem; margin-top: 0.3rem; }
        .my-trend { max-width: 460px; margin: -0.75rem auto 1.5rem; background: var(--card); border: 1px solid var(--gold);
            box-shadow: inset 4px 0 0 var(--gold); border-radius: 12px; padding: 0.6rem 0.9rem 0.5rem; }
        .my-trend[hidden] { display: none; }
        .my-trend .my-name { font-weight: 700; color: var(--deep); overflow-wrap: anywhere; font-size: 0.9rem; }
        .my-trend .trend { margin-top: 0.2rem; border-top: 0; padding-top: 0; }
'''

# The picked team's "By round" chart under the picker (a copy of the one in its team card)
TREND_JS = '''
<script>
(function () {
    var sel = document.getElementById('team-pick');
    var panel = document.getElementById('my-trend');
    if (!sel || !panel) return;
    function show() {
        var team = sel.value;
        panel.innerHTML = '';
        var card = Array.prototype.filter.call(document.querySelectorAll('.team-card'), function (c) {
            return c.getAttribute('data-team') === team; })[0];
        var tr = team && card && card.querySelector('.trend');
        if (!tr) { panel.hidden = true; return; }
        var name = document.createElement('div');
        name.className = 'my-name';
        name.textContent = team;
        panel.appendChild(name);
        panel.appendChild(tr.cloneNode(true));
        panel.hidden = false;
    }
    sel.addEventListener('change', show);
    var clear = document.getElementById('team-clear');
    if (clear) clear.addEventListener('click', function () { setTimeout(show, 0); });
    show();
})();
</script>
'''


def generate_matches_html(predictions: list[dict], season: int, round_num: int,
                          nav: Optional[dict] = None, n_sims: Optional[int] = None,
                          places: Optional[dict] = None, stakes: Optional[list] = None, featured='auto') -> str:
    """Generate HTML report with match predictions.
    places: team -> current place; matches are then ordered by table position (top boards first).
    stakes: match_stakes() of a --full-season run - the biggest-swing match goes first with the full panel,
    other matches get an 'At stake' line when a team's chance moves STAKE_SWING_MIN+. None = no stake section."""

    if places:
        # By table position: the match of the highest-placed teams first (Swiss 'top boards')
        predictions.sort(key=lambda p: _table_key(p['team_a'], p['team_b'], places))
    else:
        # Most uncertain matches first
        predictions.sort(key=lambda p: abs(p['team_a_win_prob'] - 0.5))
    stake_by_pair = {frozenset((m['a'], m['b'])): m for m in (stakes or [])}
    if featured == 'auto':
        featured = featured_stake(stakes, places or {}) if stakes else None
    if featured:
        key = frozenset((featured['a'], featured['b']))
        predictions.sort(key=lambda p: frozenset((p['team_a'], p['team_b'])) != key)   # stable: rest keep their order

    css = '''
        .predictions-container { display: flex; flex-direction: column; gap: 1.25rem; }
        .match-card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
        .match-header {
            display: flex; justify-content: space-between; align-items: center; gap: 0.75rem;
            padding: 0.9rem 1.25rem; background: var(--card-2); border-bottom: 1px solid var(--line);
        }
        .team { font-weight: 700; font-size: 1.05rem; flex: 1 1 0; min-width: 0; overflow-wrap: anywhere; }
        .team-a { color: var(--team-a); }
        .team-b { color: var(--team-b); text-align: right; }
        .vs { color: var(--muted); font-size: 0.85rem; flex: none; }
        .prediction-bar { display: flex; height: 36px; margin: 1rem 1.25rem; border-radius: 6px; overflow: hidden; }
        .prob-segment {
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 0.85rem; color: #fff; min-width: 40px;
        }
        .prob-a { background: var(--team-a); }
        .prob-draw { background: var(--draw); color: var(--ink); }
        .prob-b { background: var(--team-b); }
        .bar-key { display: flex; justify-content: space-between; margin: -0.6rem 1.25rem 0; font-size: 0.72rem; color: var(--muted); }
        .match-details { padding: 1rem 1.25rem; display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
        .detail-section { background: var(--card-2); border-radius: 8px; padding: 0.65rem 1rem; }
        .detail-title { font-size: 0.72rem; color: var(--muted); margin-bottom: 0.35rem; text-transform: uppercase; letter-spacing: 0.05em; }
        .expected-score { font-size: 1.4rem; font-weight: 700; }
        .current-score { font-size: 1.2rem; font-weight: 600; color: var(--ink); }
        .boards-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.5rem; padding: 0 1.25rem 1rem; }
        .board-card { background: var(--card-2); border-radius: 6px; padding: 0.45rem 0.5rem; font-size: 0.75rem; min-width: 0; }
        .board-players { display: flex; flex-direction: column; gap: 0.2rem; }
        .board-player { display: flex; align-items: center; gap: 0.3rem; min-width: 0; }
        .player-name { flex: 1 1 auto; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .player-rating { color: var(--muted); font-size: 0.7rem; flex: none; }
        .color-indicator { flex: none; display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
        .color-white { background: #fff; border: 1px solid #7a8f95; }
        .color-black { background: #2a3b40; border: 1px solid #2a3b40; }
        .score-result { flex: none; font-weight: 700; font-size: 0.85rem; min-width: 1.5rem; text-align: center; }
        .score-prediction { flex: none; font-size: 0.72rem; min-width: 2.2rem; text-align: center; }
        @media (max-width: 768px) {
            .boards-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 0.75rem 0.75rem; }
            .match-details { grid-template-columns: 1fr; padding: 0.75rem; }
            .match-header { padding: 0.75rem; }
            .prediction-bar { margin: 0.9rem 0.75rem; }
            .bar-key { margin: -0.5rem 0.75rem 0; }
        }
        @media (max-width: 480px) {
            .boards-grid { grid-template-columns: minmax(0, 1fr); }
        }
'''
    sims = f'{n_sims:,} simulations' if n_sims else 'Monte Carlo simulation'
    body = '    <div class="predictions-container">\n'

    for pred in predictions:
        team_a = pred['team_a']
        team_b = pred['team_b']
        a_p, b_p, d_p = pred['team_a_win_prob'], pred['team_b_win_prob'], pred['draw_prob']
        a_exp = pred['team_a_expected']
        b_exp = pred['team_b_expected']
        a_curr, b_curr = pred['current_score']
        remaining = pred['games_remaining']

        segments = ''
        for cls, p in (('prob-a', a_p), ('prob-draw', d_p), ('prob-b', b_p)):
            if p > 0:
                segments += f'<div class="prob-segment {cls}" style="width: {p * 100}%">{fmt_pct(p)}</div>'

        stake = stake_by_pair.get(frozenset((team_a, team_b)))
        card_open = '<div class="match-card">'
        stake_html = ''
        if stake is not None and featured and frozenset((stake['a'], stake['b'])) == key:
            card_open = '<div class="match-card featured" id="stakes">'
            stake_html = '\n            ' + _stake_feature_block(stake, team_a, team_b)
        elif stake is not None:
            line = _stake_line_block(stake, team_a, team_b)
            stake_html = '\n            ' + line if line else ''

        body += f'''
        {card_open}
            <div class="match-header">
                <div class="team team-a">{_esc(team_a)}</div>
                <div class="vs">vs</div>
                <div class="team team-b">{_esc(team_b)}</div>
            </div>
            <div class="prediction-bar">{segments}</div>
            <div class="bar-key"><span>win</span><span>tied match</span><span>win</span></div>{stake_html}
            <div class="match-details">
                <div class="detail-section">
                    <div class="detail-title">Expected final score</div>
                    <div class="expected-score">
                        <span style="color: var(--team-a)">{a_exp:.1f}</span>
                        <span style="color: var(--muted)"> - </span>
                        <span style="color: var(--team-b)">{b_exp:.1f}</span>
                    </div>
                </div>
                <div class="detail-section">
                    <div class="detail-title">Score now ({8 - remaining}/8 played)</div>
                    <div class="current-score">{a_curr:.1f} - {b_curr:.1f}</div>
                </div>
            </div>
            <div class="boards-grid">
'''

        for pairing in pred['pairings']:
            white = pairing.white
            black = pairing.black
            white_boosted = Player(username=white.username, rating=white.rating + 25, rd=white.rd)
            white_exp = expected_score(white_boosted, black)
            black_exp = 1 - white_exp

            if pairing.is_played:
                if pairing.double_forfeit:
                    white_score, black_score = "0F", "0F"
                elif pairing.result == 1:
                    white_score, black_score = "1", "0"
                elif pairing.result == 0:
                    white_score, black_score = "0", "1"
                else:
                    white_score, black_score = "½", "½"
                show_result = True
            else:
                white_score = fmt_pct(white_exp)
                black_score = fmt_pct(black_exp)
                show_result = False

            if pairing.white_team == team_a:
                a_player, b_player = white, black
                a_score, b_score = white_score, black_score
                a_color_class, b_color_class = "color-white", "color-black"
            else:
                a_player, b_player = black, white
                a_score, b_score = black_score, white_score
                a_color_class, b_color_class = "color-black", "color-white"

            score_class = "score-result" if show_result else "score-prediction"

            body += f'''
                <div class="board-card">
                    <div class="board-players">
                        <div class="board-player">
                            <span class="color-indicator {a_color_class}"></span>
                            <span class="player-name" style="color: var(--team-a)">{_esc(a_player.username)}</span>
                            <span class="{score_class}" style="color: var(--team-a)">{a_score}</span>
                            <span class="player-rating">{a_player.rating}</span>
                        </div>
                        <div class="board-player">
                            <span class="color-indicator {b_color_class}"></span>
                            <span class="player-name" style="color: var(--team-b)">{_esc(b_player.username)}</span>
                            <span class="{score_class}" style="color: var(--team-b)">{b_score}</span>
                            <span class="player-rating">{b_player.rating}</span>
                        </div>
                    </div>
                </div>
'''

        body += '''
            </div>
        </div>
'''

    body += '''    </div>
    <p class="note">Bar: chance each team wins the match, or it ends 4-4. Board %: each player's expected score in an unplayed game.
    &lt;1% = above zero but under 1%.</p>
'''
    return _page(
        f'Round {round_num} match predictions - Season {season}',
        f'Round {round_num} match predictions',
        f'Season {season} · win chances for each team match · {sims} · updated {_updated()}',
        body, 'matches', round_num, nav, extra_css=css + (STAKE_MATCHES_CSS if stakes else ''))


def generate_standings_html(standings_prediction: dict, season: int, round_num: int,
                            nav: Optional[dict] = None, mode: str = 'round', n_sims: Optional[int] = None,
                            rounds_left: Optional[int] = None) -> str:
    """Predicted table: after this round (mode 'round') or at the season's end ('season' / 'preseason').
    rounds_left (season mode): rounds still to play incl. the current one - enables 'Clinched' / 'Out of reach'."""
    t = _mode_texts(mode, season, round_num, n_sims)
    page_key = 'standings' if mode == 'round' else 'season_standings'
    sorted_standings = _sorted_standings(standings_prediction)

    rows = ''
    for i, (team, stats) in enumerate(sorted_standings, 1):
        probs = stats['placement_probs']
        top1 = probs.get(1, 0)
        top3 = sum(probs.get(j, 0) for j in range(1, 4))
        rows += (f'<tr data-i="{i}" data-team="{_esc(team)}">'
                 f'<td class="rank">{i}</td>'
                 f'<td class="tl team">{_esc(team)}</td>'
                 f'<td class="dim">{stats["current_standing"]}</td>'
                 f'{_prob_cell(top1, _settled_by_points(standings_prediction, team, 1, rounds_left))}'
                 f'{_prob_cell(top3, _settled_by_points(standings_prediction, team, 3, rounds_left))}'
                 f'<td class="dim">{stats["expected_match_points"]:.1f}</td>'
                 f'<td class="dim">{stats["expected_game_points"]:.1f}</td></tr>\n')

    clinch_note = (' Clinched / Out of reach = already settled by match points alone (tiebreaks not counted).'
                   if rounds_left else '')
    end = 'after this round' if mode == 'round' else 'at the end of the season'
    body = f'''{_picker_html(standings_prediction.keys())}
    <div class="box" style="max-width: 820px; margin: 0 auto;">
        <table class="std">
            <thead><tr>
                <th>#</th><th class="tl">Team</th><th title="Place now">Now</th>
                <th>{t['first']}</th><th>Top 3</th>
                <th title="Average match points {end}">MP</th><th title="Average game points {end}">GP</th>
            </tr></thead>
            <tbody data-pin-list>
{rows}            </tbody>
        </table>
    </div>
    <p class="note" style="max-width: 820px; margin: 0.6rem auto 0;"># = projected order. MP / GP = average match / game points {end}.
    &lt;1% = happened in only a few simulations, – = never happened.{clinch_note}</p>
'''
    return _page(t['standings_title'], t['standings_h1'], t['standings_sub'], body, page_key, round_num, nav,
                 script=PICKER_JS)


def generate_distribution_html(standings_prediction: dict, season: int, round_num: int,
                               nav: Optional[dict] = None, mode: str = 'round', n_sims: Optional[int] = None,
                               rounds_left: Optional[int] = None, stake_card: str = '',
                               history_rounds: Optional[dict] = None) -> str:
    """Chances of each place: summary bands first (phone friendly), every place behind a toggle, then team cards.
    stake_card: HTML of the 'Biggest potential swing this round' card (stake_season_card), shown above the summary table.
    history_rounds: this season's prediction history {round: entry} (Season finish only) - this-week arrows against
    this round's entry and a 'By round' chart per team. None / empty = neither."""
    t = _mode_texts(mode, season, round_num, n_sims)
    page_key = 'distribution' if mode == 'round' else 'season_distribution'
    sorted_standings = _sorted_standings(standings_prediction)
    num_teams = len(sorted_standings)

    # Bands (all 'higher is better'): 1st/Title and Top 3 always; Top 8 / Top 15 only when there are enough teams
    bands = [(t['first'], 'top', 1), ('Top 3', 'top', 3)]
    if num_teams >= 20:
        bands += [('Top 8', 'top', 8), ('Top 15', 'top', 15)]
    band_keys = {1: 'title', 3: 'top3', 8: 'top8', 15: 'top15'}
    history_rounds = history_rounds if mode == 'season' and history_rounds else {}
    week_ref = history_rounds.get(round_num, {}).get('teams') or None

    css = '''
        .wrap { max-width: 860px; margin: 0 auto; }
        details.matrix { margin-top: 1.5rem; }
        details.matrix > summary {
            cursor: pointer; display: inline-block; list-style: none;
            padding: 0.45rem 1rem; border-radius: 8px; border: 1px solid var(--teal);
            color: var(--teal); background: var(--card); font-weight: 600; font-size: 0.9rem;
        }
        details.matrix > summary::-webkit-details-marker { display: none; }
        details.matrix > summary::before { content: '▸ '; }
        details.matrix[open] > summary::before { content: '▾ '; }
        details.matrix .box { margin-top: 0.75rem; }
        table.heat { border-collapse: collapse; font-size: 0.78rem; }
        table.heat th, table.heat td { padding: 0.4rem 0.35rem; text-align: center; border: 1px solid var(--line); min-width: 2.6rem; }
        table.heat th { background: var(--card-2); color: var(--muted); font-weight: 600; }
        table.heat .stick { position: sticky; left: 0; z-index: 1; background: var(--card); text-align: left;
            min-width: 9rem; max-width: 14rem; white-space: normal; overflow-wrap: anywhere; font-weight: 600; }
        table.heat th.stick { background: var(--card-2); }
        table.heat tr.mine .stick { background: var(--mine); }
        table.std.wide th.tl, table.std.wide td.team { position: sticky; left: 0; z-index: 1; background: var(--card); }
        table.std.wide th.tl { background: var(--card-2); }
        table.std.wide tr.mine td.team { background: var(--mine); }
        @media (max-width: 600px) {
            table.std.wide td.team { min-width: 7.5rem; max-width: 9rem; box-shadow: 1px 0 0 var(--line); }
        }
        .team-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 380px), 1fr)); gap: 1rem; }
        .team-card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 0.9rem 1rem; min-width: 0; }
        .team-card-header { display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; margin-bottom: 0.5rem; }
        .team-card-name { font-weight: 700; color: var(--ink); overflow-wrap: anywhere; min-width: 0; }
        .team-card-stats { font-size: 0.8rem; color: var(--muted); white-space: nowrap; }
        .team-chart { width: 100%; height: auto; display: block; }
        .team-card-footer { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
        .prob-badge { padding: 0.2rem 0.55rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; background: var(--card-2); color: var(--deep); }
'''

    # --- summary table (season modes: + expected final match / game points; team column sticky when it scrolls) ---
    with_points = mode != 'round'
    head = ''.join(f'<th>{_esc(name)}</th>' for name, _, _ in bands)
    if with_points:
        head += '<th title="Average match points at the end">MP</th><th title="Average game points at the end">GP</th>'
    rows = ''
    for i, (team, stats) in enumerate(sorted_standings, 1):
        probs = stats['placement_probs']
        cells = ''
        for name, side, k in bands:
            places = range(1, k + 1) if side == 'top' else range(num_teams - k + 1, num_teams + 1)
            p = sum(probs.get(j, 0) for j in places)
            settled = _settled_by_points(standings_prediction, team, k, rounds_left) if side == 'top' else None
            cell = _prob_cell(p, settled)
            arrow = _week_arrow(p, week_ref, team, band_keys[k]) if side == 'top' else ''
            cells += cell[:-5] + arrow + '</td>' if arrow else cell
        if with_points:
            cells += (f'<td class="dim">{stats["expected_match_points"]:.1f}</td>'
                      f'<td class="dim">{stats["expected_game_points"]:.1f}</td>')
        rows += (f'<tr data-i="{i}" data-team="{_esc(team)}"><td class="tl team">{_esc(team)}</td>'
                 f'<td class="dim">{stats["current_standing"]}</td>{cells}</tr>\n')

    clinch_note = (' Clinched / Out of reach = already settled by match points alone (tiebreaks not counted).'
                   if rounds_left else '')
    points_note = ' MP / GP = average match / game points at the end of the season.' if with_points else ''
    if stake_card:
        css += STAKE_SEASON_CSS
    script = PICKER_JS
    my_trend = week_key = ''
    if history_rounds:
        css += WEEK_CSS
        script += TREND_JS
        my_trend = '\n    <div class="my-trend" id="my-trend" hidden></div>'
    if week_ref:
        week_key = ('\n    <p class="wk-key"><span class="wk">▲</span>/<span class="wk">▼</span> = change this week, since the '
                    f'round started on Monday; shown from {round(WEEK_ARROW_MIN * 100)} points '
                    f'(<span class="wk big">▲{round(WEEK_ARROW_BIG * 100)}</span> = {round(WEEK_ARROW_BIG * 100)} or more).</p>')
    body = f'''{_picker_html(standings_prediction.keys())}{my_trend}{stake_card}
    <div class="wrap">{week_key}
    <div class="box">
        <table class="std{' wide' if with_points else ''}">
            <thead><tr><th class="tl">Team</th><th title="Place now">Now</th>{head}</tr></thead>
            <tbody data-pin-list>
{rows}            </tbody>
        </table>
    </div>
    <p class="note">Teams in projected order.{points_note} &lt;1% = happened in only a few simulations, – = never happened.{clinch_note}</p>
    </div>
'''

    # --- every place (toggle) ---
    pos_head = ''.join(f'<th>{pos}</th>' for pos in range(1, num_teams + 1))
    mrows = ''
    for i, (team, stats) in enumerate(sorted_standings, 1):
        probs = stats['placement_probs']
        cells = ''
        for pos in range(1, num_teams + 1):
            cells += _prob_cell(probs.get(pos, 0))
        mrows += (f'<tr data-i="{i}" data-team="{_esc(team)}"><td class="stick">{_esc(team)}</td>'
                  f'<td class="dim">{stats["current_standing"]}</td>{cells}</tr>\n')
    body += f'''
    <details class="matrix">
        <summary>Show every place</summary>
        <div class="box">
            <table class="heat">
                <thead><tr><th class="stick">Team</th><th>Now</th>{pos_head}</tr></thead>
                <tbody data-pin-list>
{mrows}                </tbody>
            </table>
        </div>
    </details>

    <h2>Each team's chances</h2>
    <div class="team-cards" data-pin-list>
'''

    # --- team cards with bar charts ---
    for i, (team, stats) in enumerate(sorted_standings, 1):
        current = stats['current_standing']
        probs = stats['placement_probs']
        if probs:
            most_likely_pos = max(probs.keys(), key=lambda p: probs[p])
        else:
            most_likely_pos = current
        max_prob = max(probs.values()) if probs else 0.01

        chart_width, chart_height = 400, 120
        bar_width = chart_width / num_teams - 4 if num_teams < 40 else chart_width / num_teams - 1.5
        svg_bars = ''
        svg_labels = ''
        for pos in range(1, num_teams + 1):
            prob = probs.get(pos, 0)
            bar_height = (prob / max_prob) * 80 if max_prob > 0 else 0
            x = (pos - 1) * (chart_width / num_teams) + 1
            y = 90 - bar_height
            if prob >= max_prob * 0.8:
                fill = '#1a4a5c'
            elif prob >= max_prob * 0.5:
                fill = '#2c5f6f'
            elif prob >= max_prob * 0.2:
                fill = '#5f9aaa'
            else:
                fill = '#a8d5e2'
            if bar_height > 0:
                svg_bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{fill}" rx="1.5"/>'
            if prob >= 0.05 and bar_height > 12:
                svg_bars += (f'<text x="{x + bar_width / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle" font-size="9" '
                             f'fill="#5f7c86">{prob * 100:.0f}</text>')
            if pos == 1 or pos % 5 == 0 or pos == num_teams:
                svg_labels += (f'<text x="{x + bar_width / 2:.1f}" y="105" text-anchor="middle" font-size="10" '
                               f'fill="#5f7c86">{pos}</text>')

        top1 = probs.get(1, 0)
        top3 = sum(probs.get(j, 0) for j in range(1, 4))
        trend = _trend_block(team, history_rounds, round_num, {'title': top1, 'top3': top3, 'top15': min(1.0, sum(probs.get(j, 0) for j in range(1, 16)))}) if history_rounds else ''
        body += f'''
        <div class="team-card" data-i="{i}" data-team="{_esc(team)}">
            <div class="team-card-header">
                <span class="team-card-name">{_esc(team)}</span>
                <span class="team-card-stats">Now {current} → likely {most_likely_pos}</span>
            </div>
            <svg viewBox="-4 -12 {chart_width + 10} {chart_height + 12}" class="team-chart" role="img" aria-label="Chance of each place for {_esc(team)}">
                {svg_bars}
                {svg_labels}
            </svg>{trend}
            <div class="team-card-footer">
                <span class="prob-badge">{t['first']} {fmt_pct(top1)}</span>
                <span class="prob-badge">Top 3 {fmt_pct(top3)}</span>
            </div>
        </div>
'''

    trend_note = (" By round: each point is that round's projection when it started (Monday); Now = latest update."
                  if history_rounds else '')
    body += f'''    </div>
    <p class="note">Bars: chance of each place (numbers in %), places 1 to the last left to right.{trend_note}</p>
'''
    return _page(t['dist_title'], t['dist_h1'], t['dist_sub'], body, page_key, round_num, nav,
                 extra_css=css, script=script)


def generate_season_forward_html(season: int, round_num: int, target: str) -> str:
    """predictions_season_standings.html: the season table is now part of Season finish - forward old links there."""
    t = _esc(target)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="0; url={t}">
    <link rel="canonical" href="{t}">
    <title>Season finish - Season {season}</title>
    <style>
        {get_common_css()}
        .fwd {{ max-width: 560px; margin: 3rem auto; background: var(--card); border: 1px solid var(--line);
               border-radius: 12px; padding: 1.5rem; text-align: center; }}
        .fwd a {{ color: var(--teal); font-weight: 700; }}
    </style>
</head>
<body>
    <nav class="topbar"><a class="back" href="index.html">← The 4545 Chronicles</a></nav>
    <main class="page">
        <div class="fwd"><a href="{t}">The season table is now part of Season finish →</a></div>
    </main>
</body>
</html>
'''


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
    final_round: int = 8,
    stakes_out: Optional[dict] = None
) -> dict:
    """
    Simulate the entire remaining season using Swiss pairings.
    
    Returns:
        standings_prediction: {team: {placement_probs, expected_placement, ...}}
    stakes_out (optional dict, filled in): for each match of the current round, how many simulated seasons
        ended with each result of that match and how often each of its teams then won the title / finished top 3
        ("What's at stake"; read with match_stakes()). Only counts - the simulations themselves are unchanged.
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
    
    # Ensure all teams from current round are included (important for round 1)
    for team in team_rosters.keys():
        if team not in standings_before:
            standings_before[team] = {
                'match_points': 0,
                'game_points': 0.0,
                'games_won': 0,
                'matches': [],
                'opponents': {}
            }
    
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
    # Use full tiebreakers including H2H
    default_stats = {'match_points': 0, 'game_points': 0.0, 'games_won': 0, 'sonneborn_berger': 0.0, 'opponents': {}}
    current_sorted = sort_standings([(team, {**default_stats, **standings_before.get(team, {})}) 
         for team in teams])
    current_placements = {team: i + 1 for i, (team, _) in enumerate(current_sorted)}
    
    rounds_to_simulate = final_round - current_round + 1
    
    print(f"\nSimulating {rounds_to_simulate} rounds ({current_round} through {final_round})...")
    print(f"Running {num_simulations:,} full-season simulations\n")
    
    progress_interval = max(1, num_simulations // 20)

    # What's at stake: per current-round match (teams in alphabetical order, as in the loop below)
    # n[o] = simulated seasons with result o of that match (0 = first team won, 1 = 4-4, 2 = second team won),
    # title[team][o] / top3[team][o] = of those, seasons the team finished 1st / in the top 3
    stake_matches = None
    if stakes_out is not None:
        stake_matches = {}
        for matchup in current_matchups:
            if matchup:
                a, b = sorted((matchup[0].white_team, matchup[0].black_team))
                stake_matches[(a, b)] = {'a': a, 'b': b, 'n': [0, 0, 0],
                                         'title': {a: [0, 0, 0], b: [0, 0, 0]},
                                         'top3': {a: [0, 0, 0], b: [0, 0, 0]}}
    
    for sim in range(num_simulations):
        # Progress display
        if sim % progress_interval == 0 or sim == num_simulations - 1:
            pct = (sim + 1) / num_simulations * 100
            bar_len = 30
            filled = int(bar_len * (sim + 1) // num_simulations)
            bar = '█' * filled + '░' * (bar_len - filled)
            sys.stdout.write(f"\r  Progress: [{bar}] {pct:5.1f}% ({sim+1:,}/{num_simulations:,})")
            sys.stdout.flush()
        
        # Initialize simulation state with all tiebreaker fields
        sim_standings = {}
        for team in teams:
            base = standings_before.get(team, {})
            sim_standings[team] = {
                'match_points': base.get('match_points', 0),
                'game_points': base.get('game_points', 0.0),
                'games_won': base.get('games_won', 0),
                'matches': [],  # Track (opponent, match_pts) for SB calculation
                'opponents': dict(base.get('opponents', {}))  # Track H2H scores
            }
        sim_matchup_history = historical_matchups.copy()
        sim_color_history = {}  # Will be set after first simulated round
        round_outcomes = []     # (stake match, result) of this simulated season's current round
        
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
                team_a_wins = 0
                team_b_wins = 0
                
                for pairing in matchup:
                    if pairing.double_forfeit:
                        continue  # 0-0
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
                        if game_result == 1.0:
                            team_a_wins += 1
                        elif game_result == 0.0:
                            team_b_wins += 1
                    else:
                        team_b_score += game_result
                        team_a_score += (1 - game_result)
                        if game_result == 1.0:
                            team_b_wins += 1
                        elif game_result == 0.0:
                            team_a_wins += 1
                
                # Update game points and games won
                sim_standings[team_a]['game_points'] += team_a_score
                sim_standings[team_b]['game_points'] += team_b_score
                sim_standings[team_a]['games_won'] += team_a_wins
                sim_standings[team_b]['games_won'] += team_b_wins
                
                # Update head-to-head tracking
                sim_standings[team_a]['opponents'][team_b] = sim_standings[team_a]['opponents'].get(team_b, 0) + team_a_score
                sim_standings[team_b]['opponents'][team_a] = sim_standings[team_b]['opponents'].get(team_a, 0) + team_b_score
                
                # Update match points and track for SB
                if team_a_score > team_b_score:
                    sim_standings[team_a]['match_points'] += 2
                    sim_standings[team_a]['matches'].append((team_b, 2))
                    sim_standings[team_b]['matches'].append((team_a, 0))
                elif team_b_score > team_a_score:
                    sim_standings[team_b]['match_points'] += 2
                    sim_standings[team_a]['matches'].append((team_b, 0))
                    sim_standings[team_b]['matches'].append((team_a, 2))
                else:
                    sim_standings[team_a]['match_points'] += 1
                    sim_standings[team_b]['match_points'] += 1
                    sim_standings[team_a]['matches'].append((team_b, 1))
                    sim_standings[team_b]['matches'].append((team_a, 1))

                if stake_matches is not None and round_num == current_round:
                    outcome = 0 if team_a_score > team_b_score else (2 if team_b_score > team_a_score else 1)
                    round_outcomes.append((stake_matches[(team_a, team_b)], outcome))
                
                # Record matchup
                matchup_key = tuple(sorted([team_a, team_b]))
                sim_matchup_history.add(matchup_key)
                
                # Update color history (for next round pairing)
                for pairing in matchup:
                    sim_color_history[pairing.white_team] = 'white'
                    sim_color_history[pairing.black_team] = 'black'
                    break  # Only need first board's colors
        
        # Calculate Sonneborn-Berger for each team
        for team in teams:
            sb_score = 0.0
            for opponent, match_pts in sim_standings[team]['matches']:
                opponent_match_pts = sim_standings[opponent]['match_points']
                sb_score += match_pts * opponent_match_pts
            sim_standings[team]['sonneborn_berger'] = sb_score
        
        # Calculate final placements using full tiebreakers (including H2H)
        final_standings = sort_standings(sim_standings.items())

        if stake_matches is not None:
            final_place = {team: place for place, (team, _) in enumerate(final_standings, 1)}
            for m, outcome in round_outcomes:
                m['n'][outcome] += 1
                for team in (m['a'], m['b']):
                    if final_place[team] <= 3:
                        m['top3'][team][outcome] += 1
                        if final_place[team] == 1:
                            m['title'][team][outcome] += 1
        
        for place, (team, stats) in enumerate(final_standings, 1):
            placement_counts[team][place] += 1
            total_placements[team] += place
            total_match_points[team] += stats['match_points']
            total_game_points[team] += stats['game_points']
    
    print("\n")  # New line after progress bar

    if stakes_out is not None:
        stakes_out.update({'round': current_round, 'n_sims': num_simulations,
                           'matches': list(stake_matches.values())})
    
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


def parse_roster_excel(excel_path: str) -> dict:
    """
    Parse team roster from Excel file.
    
    Returns dict: {team_name: [player1, player2, ..., player8]}
    Players are ordered by board (1-8).
    """
    try:
        import pandas as pd
    except ImportError:
        print("Error: pandas required for --roster. Run: pip install pandas openpyxl")
        return {}
    
    df = pd.read_excel(excel_path)
    columns = list(df.columns)
    
    # Build a map of board number -> (name_col_idx, rating_col_idx)
    # Pattern: Board N column, then possibly empty columns, then Rtg column
    board_columns = {}
    
    for board in range(1, 9):
        board_col_idx = None
        
        # Find "Board N" column
        for i, col in enumerate(columns):
            if str(col).strip() == f'Board {board}':
                board_col_idx = i
                break
        
        if board_col_idx is not None:
            board_columns[board] = board_col_idx
    
    if not board_columns:
        print(f"  ERROR: No 'Board N' columns found in {excel_path}")
        return {}
    
    # Find Team column
    team_col_idx = 0
    for i, col in enumerate(columns):
        if str(col).strip().lower() == 'team':
            team_col_idx = i
            break
    
    team_rosters = {}
    
    for idx, row in df.iterrows():
        team_name = row.iloc[team_col_idx]
        if pd.isna(team_name):
            continue
        
        team_name = str(team_name).strip()
        if not team_name or team_name.lower() == 'team':
            continue
        
        players = []
        
        for board in sorted(board_columns.keys()):
            name_idx = board_columns[board]
            player_name = row.iloc[name_idx]
            
            if pd.isna(player_name):
                continue
            
            player_name = str(player_name).strip().lower()
            if player_name:
                players.append(player_name)
        
        if players:
            team_rosters[team_name] = players
    
    return team_rosters


def simulate_full_season_from_roster(
    team_rosters: dict,
    player_ratings: dict,
    num_simulations: int = 10000,
    final_round: int = 8
) -> dict:
    """
    Simulate an entire season from scratch using team rosters.
    
    Args:
        team_rosters: {team_name: [player1, player2, ..., player8]}
        player_ratings: {username: Player}
        num_simulations: number of Monte Carlo simulations
        final_round: final round number (default 8)
    
    Returns:
        standings_prediction: {team: {placement_probs, expected_placement, ...}}
    """
    teams = list(team_rosters.keys())
    
    # Initialize standings at 0-0 for all teams
    standings_before = {
        team: {
            'match_points': 0,
            'game_points': 0.0,
            'games_won': 0,
            'matches': [],
            'opponents': {}
        }
        for team in teams
    }
    
    # No historical matchups (fresh season)
    historical_matchups = set()
    
    # Track placement counts across all simulations
    placement_counts = {team: defaultdict(int) for team in teams}
    total_placements = {team: 0.0 for team in teams}
    total_match_points = {team: 0.0 for team in teams}
    total_game_points = {team: 0.0 for team in teams}
    
    # Current placements are all tied at 1
    current_placements = {team: 1 for team in teams}
    
    rounds_to_simulate = final_round
    
    print(f"\nSimulating {rounds_to_simulate} rounds (full season from roster)...")
    print(f"Running {num_simulations:,} full-season simulations\n")
    
    progress_interval = max(1, num_simulations // 20)
    
    for sim in range(num_simulations):
        # Progress display
        if sim % progress_interval == 0 or sim == num_simulations - 1:
            pct = (sim + 1) / num_simulations * 100
            print(f"\r  Progress: {pct:5.1f}% ({sim + 1:,}/{num_simulations:,})", end='', flush=True)
        
        # Copy standings for this simulation
        sim_standings = {
            team: {
                'match_points': 0,
                'game_points': 0.0,
                'games_won': 0,
                'opponents': {}
            }
            for team in teams
        }
        
        sim_matchups = set()
        
        # Simulate each round
        for round_num in range(1, final_round + 1):
            # Swiss pairing: sort by current standings
            sorted_teams = sorted(
                teams,
                key=lambda t: (
                    -sim_standings[t]['match_points'],
                    -sim_standings[t]['game_points'],
                    random.random()  # Tiebreaker
                )
            )
            
            # Pair teams (avoiding repeats)
            round_pairings = []
            paired = set()
            
            for team in sorted_teams:
                if team in paired:
                    continue
                
                # Find best available opponent
                for opponent in sorted_teams:
                    if opponent == team or opponent in paired:
                        continue
                    
                    matchup_key = tuple(sorted([team, opponent]))
                    if matchup_key in sim_matchups:
                        continue
                    
                    # Found valid pairing
                    round_pairings.append((team, opponent))
                    paired.add(team)
                    paired.add(opponent)
                    sim_matchups.add(matchup_key)
                    break
            
            # Simulate each match
            for team_a, team_b in round_pairings:
                roster_a = team_rosters[team_a]
                roster_b = team_rosters[team_b]
                
                a_score = 0.0
                b_score = 0.0
                
                for board in range(min(len(roster_a), len(roster_b))):
                    player_a = roster_a[board]
                    player_b = roster_b[board]
                    
                    pa = player_ratings.get(player_a, Player(player_a))
                    pb = player_ratings.get(player_b, Player(player_b))
                    
                    # Alternate colors by board
                    if board % 2 == 0:
                        white, black = pa, pb
                    else:
                        white, black = pb, pa
                    
                    # Calculate probabilities
                    draw_prob = calculate_draw_probability(white, black)
                    white_win_base = expected_score(white, black)
                    
                    # Adjust for white advantage (V6 uses +20)
                    WHITE_ADVANTAGE = 20
                    white_adj = Player(white.username, white.rating + WHITE_ADVANTAGE, white.rd)
                    white_win_base = expected_score(white_adj, black)
                    
                    # Normalize for decisive games
                    white_win = white_win_base * (1 - draw_prob) / (white_win_base + (1 - white_win_base))
                    black_win = (1 - draw_prob) - white_win
                    
                    # Simulate game
                    r = random.random()
                    if r < white_win:
                        result = 1.0
                    elif r < white_win + draw_prob:
                        result = 0.5
                    else:
                        result = 0.0
                    
                    # Assign points based on who was white
                    if board % 2 == 0:
                        a_score += result
                        b_score += (1 - result)
                    else:
                        b_score += result
                        a_score += (1 - result)
                
                # Update standings
                sim_standings[team_a]['game_points'] += a_score
                sim_standings[team_b]['game_points'] += b_score
                
                if a_score > b_score:
                    sim_standings[team_a]['match_points'] += 2
                elif b_score > a_score:
                    sim_standings[team_b]['match_points'] += 2
                else:
                    sim_standings[team_a]['match_points'] += 1
                    sim_standings[team_b]['match_points'] += 1
                
                sim_standings[team_a]['opponents'][team_b] = a_score
                sim_standings[team_b]['opponents'][team_a] = b_score
        
        # Calculate final placements for this simulation
        default_stats = {'match_points': 0, 'game_points': 0.0, 'games_won': 0, 'sonneborn_berger': 0.0, 'opponents': {}}
        final_sorted = sort_standings([(team, {**default_stats, **sim_standings[team]}) for team in teams])
        
        for place, (team, _) in enumerate(final_sorted, 1):
            placement_counts[team][place] += 1
            total_placements[team] += place
            total_match_points[team] += sim_standings[team]['match_points']
            total_game_points[team] += sim_standings[team]['game_points']
    
    print()  # Newline after progress
    
    # Calculate final statistics
    standings_prediction = {}
    for team in teams:
        placement_probs = {
            place: count / num_simulations
            for place, count in placement_counts[team].items()
        }
        
        standings_prediction[team] = {
            'placement_probs': placement_probs,
            'expected_placement': total_placements[team] / num_simulations,
            'expected_match_points': total_match_points[team] / num_simulations,
            'expected_game_points': total_game_points[team] / num_simulations,
            'current_standing': current_placements[team],
            'current_match_points': 0,
            'current_game_points': 0.0,
        }
    
    return standings_prediction


# ---------------------------------------------------------------------------
# MATCH CENTRE (the matches page)
# ---------------------------------------------------------------------------
CALENDAR_URL = "https://www.lichess4545.com/team4545/season/{}/pairings/calendar.ics"
CALENDAR_KEEP_HOURS = 6     # a calendar fetched by the round run is reused by the --full-season run of the same workflow


def round_calendar_text(args, prefix: str) -> Optional[str]:
    """The league calendar (scheduled times) for the Match Centre, or None.
    --calendar FILE: that file. --fetch-calendar: ONE download, kept as <prefix>_calendar.ics (not committed) so the
    --full-season run right after it needs no second request. Otherwise that kept file when it is recent."""
    keep = Path(f"{prefix}_calendar.ics")
    if getattr(args, 'calendar', None):
        try:
            return Path(args.calendar).read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            print(f"  Calendar {args.calendar} not readable ({e}) - no scheduled times")
            return None
    if getattr(args, 'fetch_calendar', False):
        try:
            response = requests.get(CALENDAR_URL.format(args.season), timeout=30,
                                    headers={'User-Agent': '4545-chronicles-match-centre'})
            response.raise_for_status()
            text = response.text
            try:
                keep.write_text(text, encoding='utf-8')
            except OSError:
                pass
            print(f"  League calendar: {text.count('BEGIN:VEVENT')} scheduled games")
            return text
        except Exception as e:
            print(f"  Warning: league calendar not fetched ({e}) - no scheduled times on the Match Centre")
            return None
    if keep.exists() and time.time() - keep.stat().st_mtime < CALENDAR_KEEP_HOURS * 3600:
        return keep.read_text(encoding='utf-8', errors='replace')
    return None


def write_matches_page(matches_file: str, predictions: list, season_data: dict, args, prefix: str,
                       nav: Optional[dict], places: dict, stakes: Optional[list] = None,
                       featured='auto') -> None:
    """The matches page = the Match Centre (match_centre.py). Without match_centre.py, or if it fails, the old
    matches page. The numbers (chances, stakes) are the ones computed here either way. featured: the Biggest swing
    match (the round's start pick from the prediction history); 'auto' = picked from these stakes."""
    if featured == 'auto':
        featured = featured_stake(stakes, places) if stakes else None
    try:
        import match_centre
    except ImportError:
        match_centre = None
    page = None
    if match_centre is not None:
        try:
            centre = match_centre.load_data(args.season, args.round, getattr(args, 'centre_data', None))
            cal_text = round_calendar_text(args, prefix)
            calendar = match_centre.parse_calendar(cal_text) if cal_text else None
            standings = None if centre else get_standings_before_round(season_data, args.round)
            nav_links = [(label.format(r=args.round), (nav or {}).get(key) or CENTRE_FILE,
                          key == 'matches')
                         for key, _, label in PREDICTION_PAGES if key in (nav or {'matches': ''})]
            round_games = [g for g in season_data.get('games', []) if g.get('round') == args.round]
            page = match_centre.render(args.season, args.round, round_games, predictions, centre=centre,
                                       stakes=stakes, featured=featured,
                                       calendar=calendar, places=places, standings=standings, nav=nav_links,
                                       n_sims=args.simulations, swing_min=STAKE_SWING_MIN)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  Warning: Match Centre failed ({e}) - writing the plain matches page")
            page = None
    if page is None:
        page = generate_matches_html(predictions, args.season, args.round, nav=nav, n_sims=args.simulations,
                                     places=places, stakes=stakes, featured=featured)
    with open(centre_path(prefix), 'w', encoding='utf-8') as f:
        f.write(page)
    with open(matches_file, 'w', encoding='utf-8') as f:          # old address: forwards to the Match Centre
        f.write(generate_matches_forward_html())


def main():
    parser = argparse.ArgumentParser(description='Predict team match outcomes using Glicko-2 ratings')
    parser.add_argument('--season', '-s', type=int, required=True, help='Season number')
    parser.add_argument('--round', '-r', type=int, required=True, help='Round number')
    parser.add_argument('--output', '-o', type=str, default=None, help='Output prefix (creates _matches.html, _standings.html, _distribution.html)')
    parser.add_argument('--simulations', '-n', type=int, default=10000, help='Number of Monte Carlo simulations')
    parser.add_argument('--no-cache', action='store_true', help='Skip rating cache, fetch fresh from Lichess')
    parser.add_argument('--export-sims', type=str, default=None, help='Export individual simulation results to Excel file')
    parser.add_argument('--full-season', action='store_true', help='Simulate entire remaining season with Swiss pairings')
    parser.add_argument('--roster', type=str, default=None, help='Excel file with team rosters (for pre-season predictions)')
    parser.add_argument('--history', type=str, default=None,
                        help='Prediction history file (default: prediction_history.json next to this script; --full-season)')
    parser.add_argument('--fetch-calendar', action='store_true',
                        help='Match Centre: download the league calendar once for scheduled times (website workflow)')
    parser.add_argument('--calendar', type=str, default=None,
                        help='Match Centre: a saved calendar .ics instead of downloading it (tests)')
    parser.add_argument('--centre-data', type=str, default=None,
                        help='Match Centre: the Monday file (default: match_centre.json next to this script)')
    
    args = parser.parse_args()
    
    # ROSTER MODE: Pre-season prediction from Excel
    if args.roster:
        if not args.full_season:
            print("Error: --roster requires --full-season flag")
            return
        
        print(f"\n{'='*60}")
        print(f"MATCH PREDICTOR - Season {args.season} PRE-SEASON (from roster)")
        print(f"{'='*60}\n")
        
        print(f"Loading team rosters from {args.roster}...")
        team_rosters = parse_roster_excel(args.roster)
        
        if not team_rosters:
            print("Error: No teams found in roster file")
            return
        
        print(f"  Found {len(team_rosters)} teams")
        
        # Get all unique players
        usernames = set()
        for players in team_rosters.values():
            for player in players:
                usernames.add(player.lower())
        
        print(f"\nLoading ratings for {len(usernames)} players...")
        player_ratings = fetch_player_ratings(list(usernames), use_cache=not args.no_cache)
        
        print(f"\n{'='*60}")
        print("FULL SEASON SIMULATION (from roster)")
        print(f"{'='*60}")
        
        standings_prediction = simulate_full_season_from_roster(
            team_rosters, player_ratings, args.simulations
        )
        
        # Sort by expected placement
        sorted_standings = sorted(
            standings_prediction.items(),
            key=lambda x: x[1]['expected_placement']
        )
        
        print(f"\n{'#':<3} {'Team':<35} {'Proj':>5} {'1st':>6} {'Top 3':>7}")
        print("-" * 60)
        for i, (team, stats) in enumerate(sorted_standings, 1):
            top1_prob = stats['placement_probs'].get(1, 0) * 100
            top3_prob = sum(stats['placement_probs'].get(j, 0) for j in range(1, 4)) * 100
            proj = stats['expected_placement']
            print(f"{i:<3} {team[:35]:<35} {proj:>5.1f} {top1_prob:>5.0f}% {top3_prob:>6.0f}%")
        
        # Generate HTML outputs
        if args.output:
            prefix = args.output.replace('.html', '')
            
            # Standings table
            nav = prediction_nav(prefix, os.path.exists(centre_path(prefix)), True)
            # Old season-table file name: now a forwarding page to Season finish (the table is part of it)
            standings_html = generate_season_forward_html(args.season, args.round,
                                                          f"{os.path.basename(prefix)}_season_distribution.html")
            standings_file = f"{prefix}_season_standings.html"
            with open(standings_file, 'w', encoding='utf-8') as f:
                f.write(standings_html)
            print(f"\n✅ Season table (forwards to Season finish): {standings_file}")
            
            # Distribution heatmap
            distribution_html = generate_distribution_html(standings_prediction, args.season, args.round, nav=nav,
                                                           mode='preseason', n_sims=args.simulations)
            distribution_file = f"{prefix}_season_distribution.html"
            with open(distribution_file, 'w', encoding='utf-8') as f:
                f.write(distribution_html)
            print(f"✅ Season placement distribution: {distribution_file}")
        
        print(f"\n{'='*60}")
        print("PREDICTION COMPLETE")
        print(f"{'='*60}\n")
        return
    
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
        
        stakes_raw = {}
        standings_prediction = simulate_full_season(
            season_data, args.round, player_ratings, args.simulations, stakes_out=stakes_raw
        )
        # What's at stake: the same simulations split by each match's result this round
        stakes = match_stakes(stakes_raw)
        places = {team: stats['current_standing'] for team, stats in standings_prediction.items()}
        featured = featured_stake(stakes, places)
        if featured:
            print(f"Biggest potential swing right now: {featured['a']} vs {featured['b']}")
        # Prediction history: this round's reference (once) for the this-week arrows + the By-round charts
        history_rounds = None
        if args.output:
            history_rounds = update_prediction_history(args.season, args.round, season_data, matchups, player_ratings,
                                                       standings_prediction, args.simulations, args.history,
                                                       stakes=stakes, places=places)
        week_ref = (history_rounds or {}).get(args.round, {}).get('teams')
        # Biggest swing = the match picked as the round started (kept all week); its numbers on the Season finish
        # card stay live. Without a saved pick (no history file) the match picked from right now, as before.
        start_pick = (history_rounds or {}).get(args.round, {})
        if 'featured' in start_pick:
            fz = start_pick['featured']
            live = {frozenset((m['a'], m['b'])): m for m in stakes}
            featured_start = fz
            featured = (live.get(frozenset((fz['a'], fz['b']))) or fz) if fz else None
        else:
            featured_start = featured
        
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
            # Header links: round pages only if they exist next to these (GitHub Actions makes both sets)
            nav = prediction_nav(prefix, os.path.exists(centre_path(prefix)), True)
            # Rounds still to play incl. this one (simulate_full_season's default final round 8) - for Clinched labels
            rounds_left = 8 - args.round + 1
            # Old season-table file name: now a forwarding page to Season finish (the table is part of it)
            standings_html = generate_season_forward_html(args.season, args.round,
                                                          f"{os.path.basename(prefix)}_season_distribution.html")
            standings_file = f"{prefix}_season_standings.html"
            with open(standings_file, 'w', encoding='utf-8') as f:
                f.write(standings_html)
            print(f"\n✅ Season table (forwards to Season finish): {standings_file}")
            
            # Distribution heatmap - different filename
            matches_file = f"{prefix}_matches.html"
            has_matches = os.path.exists(centre_path(prefix))
            stake_card = (stake_season_card(featured, places, args.round,
                                            CENTRE_FILE if has_matches else None, week_ref)
                          if featured else '')
            distribution_html = generate_distribution_html(standings_prediction, args.season, args.round, nav=nav,
                                                           mode='season', n_sims=args.simulations, rounds_left=rounds_left,
                                                           stake_card=stake_card, history_rounds=history_rounds)
            distribution_file = f"{prefix}_season_distribution.html"
            with open(distribution_file, 'w', encoding='utf-8') as f:
                f.write(distribution_html)
            print(f"✅ Season placement distribution: {distribution_file}")

            # Matches page (written by the round run - GitHub Actions runs it first): rewrite it with the stakes.
            # Same match simulation as the round run; done after the season simulations, so those stay unchanged.
            if has_matches:
                predictions = [simulate_match(pairings, args.simulations) for pairings in matchups if pairings]
                write_matches_page(matches_file, predictions, season_data, args, prefix, nav, places, stakes,
                                   featured=featured_start)
                print(f"✅ Match Centre with what's at stake: {centre_path(prefix)} (+ {matches_file} forwards to it)")
        
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
        
        # Header links: season pages only if they exist next to these (made by a --full-season run)
        nav = prediction_nav(prefix, True, os.path.exists(f"{prefix}_season_distribution.html"))

        # 1. Match predictions
        # Ordered by table position; no stakes here (they need the --full-season run, which rewrites this page)
        places = {team: stats['current_standing'] for team, stats in standings_prediction.items()}
        matches_file = f"{prefix}_matches.html"
        write_matches_page(matches_file, predictions, season_data, args, prefix, nav, places)
        print(f"\n✅ Match Centre: {centre_path(prefix)} (+ {matches_file} forwards to it)")
        
        # 2. Standings table
        standings_html = generate_standings_html(standings_prediction, args.season, args.round, nav=nav,
                                                 n_sims=args.simulations)
        standings_file = f"{prefix}_standings.html"
        with open(standings_file, 'w', encoding='utf-8') as f:
            f.write(standings_html)
        print(f"✅ Standings table: {standings_file}")
        
        # 3. Placement distribution
        distribution_html = generate_distribution_html(standings_prediction, args.season, args.round, nav=nav,
                                                       n_sims=args.simulations)
        distribution_file = f"{prefix}_distribution.html"
        with open(distribution_file, 'w', encoding='utf-8') as f:
            f.write(distribution_html)
        print(f"✅ Placement distribution: {distribution_file}")
    
    print(f"\n{'='*60}")
    print("PREDICTION COMPLETE")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
