#!/usr/bin/env python3
"""
Lichess 4545 Data Cache Module
Caches historical season data to avoid repeated API calls
"""

import os
import json
import requests
import time
from pathlib import Path


class DataCache:
    """Manages caching of historical season data"""
    
    def __init__(self, cache_dir=None):
        """Initialize cache with specified directory"""
        if cache_dir is None:
            # Use .lichess4545_cache in user's home directory
            cache_dir = Path.home() / '.lichess4545_cache'
        else:
            cache_dir = Path(cache_dir)
        
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(exist_ok=True, parents=True)
        print(f"📁 Cache directory: {self.cache_dir}")
    
    def get_cache_file(self, season):
        """Get cache file path for a season"""
        return self.cache_dir / f"season_{season}.json"
    
    def is_cached(self, season):
        """Check if season data is cached"""
        cache_file = self.get_cache_file(season)
        return cache_file.exists()
    
    def load_from_cache(self, season):
        """Load season data from cache"""
        cache_file = self.get_cache_file(season)
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except Exception as e:
            print(f"  ⚠️  Error reading cache for season {season}: {e}")
            return None
    
    def save_to_cache(self, season, data):
        """Save season data to cache"""
        cache_file = self.get_cache_file(season)
        
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            return True
        except Exception as e:
            print(f"  ⚠️  Error writing cache for season {season}: {e}")
            return False
    
    def fetch_with_cache(self, season, api_base, current_season=None, force_refresh=False):
        """
        Fetch season data, using cache for old seasons
        
        Args:
            season: Season number to fetch
            api_base: API base URL
            current_season: Current active season (will always fetch fresh)
            force_refresh: Force refresh even if cached
        """
        is_current = (current_season is not None and season == current_season)
        
        # Always fetch current season fresh
        if is_current:
            return self._fetch_from_api(season, api_base)
        
        # Check cache for historical seasons
        if not force_refresh and self.is_cached(season):
            data = self.load_from_cache(season)
            if data is not None:
                return data
        
        # Not cached or force refresh - fetch from API
        data = self._fetch_from_api(season, api_base)
        
        # Cache it for future use (only if not current season)
        if data is not None and not is_current:
            self.save_to_cache(season, data)
        
        return data
    
    def _fetch_from_api(self, season, api_base):
        """Fetch season data from API"""
        url = f"{api_base}/get_season_games/?league=team4545&season={season}"
        
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get('games', [])
        except Exception as e:
            print(f"  ⚠️  Error fetching season {season}: {e}")
            return None
    
    def clear_cache(self, season=None):
        """Clear cache for specific season or all seasons"""
        if season is not None:
            # Clear specific season
            cache_file = self.get_cache_file(season)
            if cache_file.exists():
                cache_file.unlink()
                print(f"✓ Cleared cache for season {season}")
            else:
                print(f"  No cache found for season {season}")
        else:
            # Clear all cache
            count = 0
            for cache_file in self.cache_dir.glob("season_*.json"):
                cache_file.unlink()
                count += 1
            print(f"✓ Cleared cache for {count} season(s)")
    
    def get_cache_info(self):
        """Get information about cached seasons"""
        cached_seasons = []
        total_size = 0
        
        for cache_file in sorted(self.cache_dir.glob("season_*.json")):
            season_num = int(cache_file.stem.split('_')[1])
            size = cache_file.stat().st_size
            total_size += size
            
            cached_seasons.append({
                'season': season_num,
                'size': size,
                'size_mb': size / (1024 * 1024)
            })
        
        return {
            'cached_seasons': cached_seasons,
            'count': len(cached_seasons),
            'total_size': total_size,
            'total_size_mb': total_size / (1024 * 1024)
        }
    
    def print_cache_info(self):
        """Print cache information"""
        info = self.get_cache_info()
        
        print("\n" + "="*60)
        print("CACHE INFORMATION")
        print("="*60)
        print(f"Cache directory: {self.cache_dir}")
        print(f"Cached seasons: {info['count']}")
        print(f"Total size: {info['total_size_mb']:.2f} MB")
        print()
        
        if info['cached_seasons']:
            print("Cached seasons:")
            for season_info in info['cached_seasons']:
                print(f"  Season {season_info['season']:2d}: {season_info['size_mb']:.2f} MB")
        else:
            print("  No seasons cached yet")
        
        print("="*60 + "\n")


def fetch_all_seasons_with_cache(target_season, api_base, cache_dir=None, current_season=None):
    """
    Fetch all seasons up to target_season, using cache for old seasons
    
    Args:
        target_season: Highest season to fetch
        api_base: API base URL
        cache_dir: Cache directory (None = use default)
        current_season: Current active season (None = assume target_season is current)
    
    Returns:
        List of all games from all seasons
    """
    if current_season is None:
        current_season = target_season
    
    cache = DataCache(cache_dir)
    all_games = []
    
    print(f"Fetching seasons 1-{target_season} (current season: {current_season})...")
    print()
    
    for season in range(1, target_season + 1):
        is_current = (season == current_season)
        
        if cache.is_cached(season) and not is_current:
            # Load from cache
            games = cache.load_from_cache(season)
            if games is not None:
                all_games.extend(games)
                if season % 5 == 0 or season == target_season:
                    print(f"  ✓ Season {season:2d} [CACHED] - Total games: {len(all_games)}")
                continue
        
        # Fetch from API
        source = "[LIVE]" if is_current else "[API]"
        games = cache.fetch_with_cache(season, api_base, current_season)
        
        if games is not None:
            all_games.extend(games)
            if season % 5 == 0 or season == target_season:
                print(f"  ✓ Season {season:2d} {source:8s} - Total games: {len(all_games)}")
        
        # Rate limiting
        time.sleep(0.1)
    
    print()
    print(f"✓ Fetched {len(all_games)} total games from {target_season} seasons")
    
    # Show cache info
    cache.print_cache_info()
    
    return all_games


# Command-line interface for cache management
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Lichess 4545 Cache Manager',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python data_cache.py --info                    # Show cache info
  python data_cache.py --clear                   # Clear all cache
  python data_cache.py --clear-season 45         # Clear specific season
  python data_cache.py --prefetch 46             # Pre-fetch seasons 1-46
        """
    )
    
    parser.add_argument('--info', action='store_true',
                       help='Show cache information')
    parser.add_argument('--clear', action='store_true',
                       help='Clear all cached data')
    parser.add_argument('--clear-season', type=int, metavar='N',
                       help='Clear specific season from cache')
    parser.add_argument('--prefetch', type=int, metavar='N',
                       help='Pre-fetch and cache seasons 1-N')
    parser.add_argument('--cache-dir', type=str, metavar='DIR',
                       help='Custom cache directory')
    
    args = parser.parse_args()
    
    cache = DataCache(args.cache_dir)
    
    if args.info:
        cache.print_cache_info()
    
    elif args.clear:
        confirm = input("Clear all cached data? (y/N): ")
        if confirm.lower() == 'y':
            cache.clear_cache()
        else:
            print("Cancelled")
    
    elif args.clear_season:
        cache.clear_cache(args.clear_season)
    
    elif args.prefetch:
        print(f"Pre-fetching seasons 1-{args.prefetch}...")
        api_base = "https://www.lichess4545.com/api"
        fetch_all_seasons_with_cache(args.prefetch, api_base, args.cache_dir)
        print("\n✅ Pre-fetch complete!")
    
    else:
        parser.print_help()
