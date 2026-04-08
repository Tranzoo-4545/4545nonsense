#!/usr/bin/env python3
"""
Lichess4545 Season Standings Calculator

Reconstructs team standings for each season using the league API.
Calculates match points, game points, and tiebreakers.

Usage:
    py standings.py --season 46           # Show standings for season 46
    py standings.py --all 47              # Generate standings for all seasons up to 47
    py standings.py --season 46 --json    # Output as JSON
    py standings.py --explore 46          # Explore data structure for a season
"""

import requests
import argparse
import sys
import time
import json
from pathlib import Path
from collections import defaultdict

# League API
LEAGUE_API_URL = "https://www.lichess4545.com/api/get_season_games/?league=team4545&include_unplayed=true&season={}"

# Cache directory (shared with other scripts)
CACHE_DIR = Path.home() / '.lichess4545_cache' / 'retention'

# Request delay
REQUEST_DELAY = 1.0


# =============================================================================
# DATA FETCHING
# =============================================================================

def get_cache_path(season):
    return CACHE_DIR / f"season_{season}_with_unplayed.json"


def load_season(season):
    """Load season from cache or fetch from API."""
    cache_file = get_cache_path(season)
    
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    
    # Fetch from API
    print(f"Fetching season {season} from API...")
    url = LEAGUE_API_URL.format(season)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()
    
    # Cache it
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    
    return data


def explore_season_data(season):
    """Explore the data structure of a season."""
    data = load_season(season)
    
    print(f"\n=== Season {season} Data Structure ===\n")
    
    # Top level keys
    print(f"Top-level keys: {list(data.keys())}")
    print()
    
    games = data.get('games', [])
    print(f"Number of games: {len(games)}")
    print()
    
    if games:
        print("Sample game (first one):")
        print(json.dumps(games[0], indent=2, default=str))
        print()
        
        # Find all unique keys across games
        all_keys = set()
        for game in games:
            all_keys.update(game.keys())
        print(f"All game keys: {sorted(all_keys)}")
        print()
        
        # Check for team-related fields
        sample = games[0]
        print("Team-related fields in sample:")
        for key in ['white_team', 'black_team', 'team', 'white_team_name', 'black_team_name']:
            if key in sample:
                print(f"  {key}: {sample[key]}")
        
        # Look for any field containing 'team'
        print("\nAll fields containing 'team':")
        for key in sample.keys():
            if 'team' in key.lower():
                print(f"  {key}: {sample[key]}")


# =============================================================================
# STANDINGS CALCULATION
# =============================================================================

def calculate_standings(season_data):
    """
    Calculate team standings from season data.
    
    Returns list of teams sorted by tiebreakers:
    1. Match points
    2. Game points
    3. Head-to-head (TODO: complex)
    4. Number of games won
    5. Sonneborn-Berger (TODO: complex)
    """
    games = season_data.get('games', [])
    
    # Group games by round and match (team vs team)
    rounds = defaultdict(list)
    
    for game in games:
        round_num = game.get('round')
        if round_num:
            rounds[round_num].append(game)
    
    # Track team stats
    teams = defaultdict(lambda: {
        'match_points': 0,
        'game_points': 0.0,
        'games_won': 0,
        'games_drawn': 0,
        'games_lost': 0,
        'matches': [],  # For Sonneborn-Berger
        'opponents': defaultdict(float),  # For head-to-head
    })
    
    # Process each round
    for round_num in sorted(rounds.keys()):
        round_games = rounds[round_num]
        
        # Group by match (white_team vs black_team)
        matches = defaultdict(list)
        
        for game in round_games:
            white_team = game.get('white_team') or game.get('white_team_name', 'Unknown')
            black_team = game.get('black_team') or game.get('black_team_name', 'Unknown')
            
            if white_team and black_team:
                # Normalize match key (alphabetical order)
                match_key = tuple(sorted([white_team, black_team]))
                matches[match_key].append(game)
        
        # Calculate match results
        for (team_a, team_b), match_games in matches.items():
            team_a_points = 0.0
            team_b_points = 0.0
            
            for game in match_games:
                white_team = game.get('white_team') or game.get('white_team_name')
                result = game.get('result', '')
                
                # White wins (normal or by forfeit)
                if result in ('1-0', '1X-0F'):
                    if white_team == team_a:
                        team_a_points += 1
                        teams[team_a]['games_won'] += 1
                        teams[team_b]['games_lost'] += 1
                    else:
                        team_b_points += 1
                        teams[team_b]['games_won'] += 1
                        teams[team_a]['games_lost'] += 1
                # Black wins (normal or by forfeit)
                elif result in ('0-1', '0F-1X'):
                    if white_team == team_a:
                        team_b_points += 1
                        teams[team_b]['games_won'] += 1
                        teams[team_a]['games_lost'] += 1
                    else:
                        team_a_points += 1
                        teams[team_a]['games_won'] += 1
                        teams[team_b]['games_lost'] += 1
                # Draws (normal or scheduling draw)
                elif result in ('1/2-1/2', '1/2Z-1/2Z'):
                    team_a_points += 0.5
                    team_b_points += 0.5
                    teams[team_a]['games_drawn'] += 1
                    teams[team_b]['games_drawn'] += 1
                # Double forfeit - both get 0
                elif result == '0F-0F':
                    # No points awarded, but count as a game played
                    teams[team_a]['games_lost'] += 1
                    teams[team_b]['games_lost'] += 1
            
            # Record game points
            teams[team_a]['game_points'] += team_a_points
            teams[team_b]['game_points'] += team_b_points
            
            # Calculate match points
            if team_a_points > team_b_points:
                teams[team_a]['match_points'] += 2
                teams[team_a]['matches'].append((team_b, 2))
                teams[team_b]['matches'].append((team_a, 0))
            elif team_b_points > team_a_points:
                teams[team_b]['match_points'] += 2
                teams[team_b]['matches'].append((team_a, 2))
                teams[team_a]['matches'].append((team_b, 0))
            else:
                teams[team_a]['match_points'] += 1
                teams[team_b]['match_points'] += 1
                teams[team_a]['matches'].append((team_b, 1))
                teams[team_b]['matches'].append((team_a, 1))
            
            # Head-to-head tracking
            teams[team_a]['opponents'][team_b] += team_a_points
            teams[team_b]['opponents'][team_a] += team_b_points
    
    # Calculate Sonneborn-Berger
    for team_name, team_data in teams.items():
        sb_score = 0.0
        for opponent, match_pts in team_data['matches']:
            opponent_match_pts = teams[opponent]['match_points']
            sb_score += match_pts * opponent_match_pts
        team_data['sonneborn_berger'] = sb_score
    
    # Sort by tiebreakers
    standings = []
    for team_name, team_data in teams.items():
        # Count match results
        matches_won = sum(1 for m in team_data['matches'] if m[1] == 2)
        matches_drawn = sum(1 for m in team_data['matches'] if m[1] == 1)
        matches_lost = sum(1 for m in team_data['matches'] if m[1] == 0)
        
        standings.append({
            'name': team_name,
            'match_points': team_data['match_points'],
            'game_points': team_data['game_points'],
            'games_won': team_data['games_won'],
            'games_drawn': team_data['games_drawn'],
            'games_lost': team_data['games_lost'],
            'matches_won': matches_won,
            'matches_drawn': matches_drawn,
            'matches_lost': matches_lost,
            'sonneborn_berger': team_data['sonneborn_berger'],
            'opponents': dict(team_data['opponents']),
        })
    
    # Sort: match points (desc), game points (desc), games won (desc), SB (desc)
    standings.sort(key=lambda t: (
        -t['match_points'],
        -t['game_points'],
        -t['games_won'],
        -t['sonneborn_berger'],
        t['name']  # Alphabetical as final tiebreaker
    ))
    
    return standings


def print_standings(standings, season):
    """Print standings in a nice table format."""
    print(f"\n{'='*70}")
    print(f"  SEASON {season} STANDINGS")
    print(f"{'='*70}")
    print()
    print(f"{'#':<3} {'Team':<30} {'MP':>4} {'GP':>6} {'W':>3} {'D':>3} {'L':>3} {'SB':>6}")
    print("-" * 70)
    
    for i, team in enumerate(standings, 1):
        print(f"{i:<3} {team['name']:<30} {team['match_points']:>4} {team['game_points']:>6.1f} "
              f"{team['games_won']:>3} {team['games_drawn']:>3} {team['games_lost']:>3} "
              f"{team['sonneborn_berger']:>6.1f}")
    
    print()


def generate_standings_html(all_standings, output_file):
    """Generate HTML file with standings for all seasons."""
    
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lichess4545 Historical Standings</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        body {
            min-height: 100vh;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            font-family: 'JetBrains Mono', monospace;
            color: #e8e8e8;
            padding: 40px 24px;
        }
        
        .container {
            max-width: 1000px;
            margin: 0 auto;
        }
        
        h1 {
            font-family: 'Space Grotesk', sans-serif;
            font-size: 36px;
            font-weight: 700;
            text-align: center;
            background: linear-gradient(135deg, #ffd700 0%, #ffaa00 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 8px;
        }
        
        .subtitle {
            text-align: center;
            color: rgba(255,255,255,0.4);
            font-size: 13px;
            letter-spacing: 2px;
            margin-bottom: 32px;
        }
        
        .season-selector {
            text-align: center;
            margin-bottom: 24px;
        }
        
        .season-selector select {
            padding: 12px 24px;
            font-size: 16px;
            font-family: inherit;
            background: rgba(0,0,0,0.4);
            border: 2px solid rgba(255,215,0,0.3);
            border-radius: 8px;
            color: #fff;
            cursor: pointer;
        }
        
        .season-selector select:focus {
            outline: none;
            border-color: #ffd700;
        }
        
        .standings-table {
            display: none;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            overflow: hidden;
        }
        
        .standings-table.active {
            display: block;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th {
            background: rgba(0,0,0,0.3);
            padding: 14px 12px;
            text-align: left;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: rgba(255,255,255,0.5);
        }
        
        th.num {
            text-align: center;
            width: 40px;
        }
        
        th.stat {
            text-align: right;
            width: 60px;
        }
        
        td {
            padding: 12px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        
        td.rank {
            text-align: center;
            font-weight: 600;
            color: rgba(255,255,255,0.4);
        }
        
        td.team-name {
            font-weight: 600;
        }
        
        td.stat {
            text-align: right;
            font-variant-numeric: tabular-nums;
        }
        
        tr:nth-child(1) td.team-name { color: #ffd700; }
        tr:nth-child(2) td.team-name { color: #c0c0c0; }
        tr:nth-child(3) td.team-name { color: #cd7f32; }
        
        tr:hover {
            background: rgba(255,215,0,0.05);
        }
        
        .legend {
            margin-top: 16px;
            padding: 16px;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            font-size: 12px;
            color: rgba(255,255,255,0.5);
        }
        
        .legend span {
            margin-right: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Historical Standings</h1>
        <p class="subtitle">LICHESS 4545 LEAGUE</p>
        
        <div class="season-selector">
            <select id="seasonSelect" onchange="showSeason(this.value)">
"""
    
    # Add season options (reverse order, newest first)
    seasons = sorted(all_standings.keys(), reverse=True)
    for season in seasons:
        html += f'                <option value="{season}">Season {season}</option>\n'
    
    html += """            </select>
        </div>
"""
    
    # Add standings tables for each season
    for season in seasons:
        standings = all_standings[season]
        active = "active" if season == seasons[0] else ""
        
        html += f"""
        <div class="standings-table {active}" id="season-{season}">
            <table>
                <thead>
                    <tr>
                        <th class="num">#</th>
                        <th>Team</th>
                        <th class="stat">MP</th>
                        <th class="stat">GP</th>
                        <th class="stat">W</th>
                        <th class="stat">D</th>
                        <th class="stat">L</th>
                        <th class="stat">SB</th>
                    </tr>
                </thead>
                <tbody>
"""
        
        for i, team in enumerate(standings, 1):
            html += f"""                    <tr>
                        <td class="rank">{i}</td>
                        <td class="team-name">{team['name']}</td>
                        <td class="stat">{team['match_points']}</td>
                        <td class="stat">{team['game_points']:.1f}</td>
                        <td class="stat">{team['games_won']}</td>
                        <td class="stat">{team['games_drawn']}</td>
                        <td class="stat">{team['games_lost']}</td>
                        <td class="stat">{team['sonneborn_berger']:.1f}</td>
                    </tr>
"""
        
        html += """                </tbody>
            </table>
        </div>
"""
    
    html += """
        <div class="legend">
            <span><strong>MP</strong> = Match Points</span>
            <span><strong>GP</strong> = Game Points</span>
            <span><strong>W/D/L</strong> = Games Won/Drawn/Lost</span>
            <span><strong>SB</strong> = Sonneborn-Berger</span>
        </div>
    </div>
    
    <script>
        function showSeason(season) {
            document.querySelectorAll('.standings-table').forEach(el => {
                el.classList.remove('active');
            });
            document.getElementById('season-' + season).classList.add('active');
        }
    </script>
</body>
</html>
"""
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"Generated: {output_file}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Calculate Lichess4545 team standings.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --explore 46         Explore data structure
  %(prog)s --season 46          Show standings for season 46
  %(prog)s --all 47             Generate HTML for all seasons
  %(prog)s --season 46 --json   Output as JSON
        """
    )
    
    parser.add_argument('--season', '-s', type=int,
                        help='Calculate standings for a specific season')
    parser.add_argument('--all', '-a', type=int,
                        help='Calculate standings for all seasons up to this number')
    parser.add_argument('--explore', '-e', type=int,
                        help='Explore data structure for a season')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON')
    parser.add_argument('--output', '-o', type=str, default='standings.html',
                        help='Output HTML file (default: standings.html)')
    
    args = parser.parse_args()
    
    if args.explore:
        explore_season_data(args.explore)
        return
    
    if args.season:
        data = load_season(args.season)
        standings = calculate_standings(data)
        
        if args.json:
            print(json.dumps(standings, indent=2))
        else:
            print_standings(standings, args.season)
        return
    
    if args.all:
        print(f"Calculating standings for seasons 1 to {args.all}...")
        all_standings = {}
        
        for season in range(1, args.all + 1):
            try:
                data = load_season(season)
                standings = calculate_standings(data)
                all_standings[season] = standings
                print(f"  Season {season}: {len(standings)} teams")
                time.sleep(0.1)
            except Exception as e:
                print(f"  Season {season}: ERROR - {e}")
        
        if args.json:
            print(json.dumps(all_standings, indent=2))
        else:
            generate_standings_html(all_standings, args.output)
        return
    
    parser.print_help()


if __name__ == '__main__':
    main()
