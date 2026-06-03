"""
Multi-source weather forecast fetcher.

Sources:
1. Open-Meteo (free, no key, global coverage, multiple models)
2. OpenWeatherMap (key required, good hourly forecasts)
3. weather.gov (free, US only, NWS forecasts)

Each source returns standardized forecast data that feeds into
the probability engine.
"""

import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from config import Config
from logger import log


@dataclass
class ForecastPoint:
    """A single forecast data point."""
    source: str           # 'open_meteo', 'openweather', 'weather_gov'
    model: str            # 'ECMWF', 'GFS', 'ICON', etc.
    location: str         # city or lat,lon
    timestamp: datetime   # forecast valid time (UTC)
    temp_c: float         # temperature in Celsius
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    precip_mm: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    confidence: float = 0.5  # 0-1 how much we trust this source
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WeatherFetcher:
    """Fetch forecasts from multiple weather APIs."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': f'WeatherSniper/{Config.VERSION}'})
        self._cache: Dict[str, Tuple[float, List[ForecastPoint]]] = {}
        self._cache_ttl = 300  # 5 minutes

    def fetch_all(self, lat: float, lon: float, city: str = '',
                  target_time: datetime = None) -> List[ForecastPoint]:
        """
        Fetch forecasts from ALL available sources for a location.
        Returns list of ForecastPoint from different models/sources.
        """
        cache_key = f"{lat:.2f},{lon:.2f},{target_time}"
        now = time.time()
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data

        results = []

        # 1. Open-Meteo (multiple models, free)
        try:
            om_results = self._fetch_open_meteo(lat, lon, city, target_time)
            results.extend(om_results)
        except Exception as e:
            log.warning(f"Open-Meteo fetch failed: {e}")

        # 2. OpenWeatherMap
        if Config.OPENWEATHER_API_KEY:
            try:
                ow_results = self._fetch_openweather(lat, lon, city, target_time)
                results.extend(ow_results)
            except Exception as e:
                log.warning(f"OpenWeatherMap fetch failed: {e}")

        # 3. weather.gov (US only)
        if -130 < lon < -60 and 24 < lat < 50:
            try:
                wg_results = self._fetch_weather_gov(lat, lon, city, target_time)
                results.extend(wg_results)
            except Exception as e:
                log.warning(f"weather.gov fetch failed: {e}")

        self._cache[cache_key] = (now, results)
        log.info(f"Fetched {len(results)} forecast points for {city or f'{lat},{lon}'}")
        return results

    def _fetch_open_meteo(self, lat: float, lon: float, city: str,
                          target_time: datetime = None) -> List[ForecastPoint]:
        """
        Open-Meteo: SINGLE batch request with all models for speed.
        Previously made 5 sequential requests — now 1 batch call.
        """
        results = []
        models = ['ecmwf_ifs04', 'gfs_seamless', 'icon_seamless',
                  'jma_seamless', 'gem_seamless']
        model_confidence = {
            'ecmwf_ifs04': 0.90, 'gfs_seamless': 0.80,
            'icon_seamless': 0.82, 'jma_seamless': 0.78, 'gem_seamless': 0.75,
        }

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            'latitude': lat,
            'longitude': lon,
            'hourly': 'temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,cloud_cover',
            'models': ','.join(models),
            'timezone': 'UTC',
            'forecast_days': 3,
        }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return results

            data = resp.json()
            hourly = data.get('hourly', {})
            times = hourly.get('time', [])
            if not times:
                return results

            for model in models:
                # Open-Meteo returns model-suffixed keys or plain keys
                temp_key = f'temperature_2m_{model}'
                temps = hourly.get(temp_key, [])
                if not temps:
                    temps = hourly.get('temperature_2m', [])
                    if not temps:
                        continue

                for i, t_str in enumerate(times):
                    if i >= len(temps) or temps[i] is None:
                        continue
                    try:
                        t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if target_time and abs((t - target_time).total_seconds()) > 7200:
                        continue

                    fp = ForecastPoint(
                        source='open_meteo',
                        model=model.replace('_seamless', '').replace('_ifs04', '').upper(),
                        location=city or f"{lat},{lon}",
                        timestamp=t,
                        temp_c=temps[i],
                        confidence=model_confidence.get(model, 0.7),
                    )
                    results.append(fp)
        except Exception as e:
            log.debug(f"Open-Meteo batch failed: {e}")

        return results

    def _fetch_openweather(self, lat: float, lon: float, city: str,
                           target_time: datetime = None) -> List[ForecastPoint]:
        """OpenWeatherMap 5-day/3-hour forecast."""
        results = []

        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            'lat': lat,
            'lon': lon,
            'appid': Config.OPENWEATHER_API_KEY,
            'units': 'metric',
        }

        resp = self.session.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return results

        data = resp.json()
        for item in data.get('list', []):
            try:
                t = datetime.fromtimestamp(item['dt'], tz=timezone.utc)
            except Exception:
                continue

            if target_time:
                diff = abs((t - target_time).total_seconds())
                if diff > 7200:
                    continue

            main = item.get('main', {})
            wind_data = item.get('wind', {})
            rain = item.get('rain', {})
            clouds_data = item.get('clouds', {})

            fp = ForecastPoint(
                source='openweather',
                model='OWM',
                location=city or f"{lat},{lon}",
                timestamp=t,
                temp_c=main.get('temp', 0),
                temp_min_c=main.get('temp_min'),
                temp_max_c=main.get('temp_max'),
                humidity_pct=main.get('humidity'),
                wind_speed_kmh=(wind_data.get('speed', 0) * 3.6),  # m/s → km/h
                precip_mm=rain.get('3h', 0),
                cloud_cover_pct=clouds_data.get('all'),
                confidence=0.75,
            )
            results.append(fp)

        return results

    def _fetch_weather_gov(self, lat: float, lon: float, city: str,
                           target_time: datetime = None) -> List[ForecastPoint]:
        """weather.gov (NWS) — US only, free, no key."""
        results = []

        # Step 1: Get gridpoint
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        resp = self.session.get(points_url, timeout=10)
        if resp.status_code != 200:
            return results

        props = resp.json().get('properties', {})
        forecast_url = props.get('forecastHourly')
        if not forecast_url:
            return results

        # Step 2: Get hourly forecast
        resp2 = self.session.get(forecast_url, timeout=10)
        if resp2.status_code != 200:
            return results

        periods = resp2.json().get('properties', {}).get('periods', [])
        for period in periods:
            try:
                t = datetime.fromisoformat(period['startTime'])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if target_time:
                diff = abs((t - target_time).total_seconds())
                if diff > 7200:
                    continue

            # Convert F to C
            temp_f = period.get('temperature', 0)
            temp_c = (temp_f - 32) * 5.0 / 9.0

            wind_str = period.get('windSpeed', '0 mph')
            try:
                wind_mph = float(wind_str.split()[0])
                wind_kmh = wind_mph * 1.609
            except Exception:
                wind_kmh = 0

            fp = ForecastPoint(
                source='weather_gov',
                model='NWS',
                location=city or f"{lat},{lon}",
                timestamp=t,
                temp_c=temp_c,
                humidity_pct=period.get('relativeHumidity', {}).get('value'),
                wind_speed_kmh=wind_kmh,
                confidence=0.82,
            )
            results.append(fp)

        return results


# ═══════════════════════════════════════════════════════════════════
# KNOWN CITY COORDINATES (for Polymarket weather markets)
# ═══════════════════════════════════════════════════════════════════
CITY_COORDS = {
    # Asia (popular on Polymarket weather markets)
    'tokyo': (35.6762, 139.6503),
    'taipei': (25.0330, 121.5654),
    'hong kong': (22.3193, 114.1694),
    'hongkong': (22.3193, 114.1694),
    'seoul': (37.5665, 126.9780),
    'singapore': (1.3521, 103.8198),
    'manila': (14.5995, 120.9842),
    'bangkok': (13.7563, 100.5018),
    'delhi': (28.6139, 77.2090),
    'mumbai': (19.0760, 72.8777),
    'shanghai': (31.2304, 121.4737),
    'beijing': (39.9042, 116.4074),
    'osaka': (34.6937, 135.5023),
    # US
    'new york': (40.7128, -74.0060),
    'nyc': (40.7128, -74.0060),
    'los angeles': (34.0522, -118.2437),
    'la': (34.0522, -118.2437),
    'chicago': (41.8781, -87.6298),
    'miami': (25.7617, -80.1918),
    'houston': (29.7604, -95.3698),
    'phoenix': (33.4484, -112.0740),
    'denver': (39.7392, -104.9903),
    'san francisco': (37.7749, -122.4194),
    'sf': (37.7749, -122.4194),
    'seattle': (47.6062, -122.3321),
    'dallas': (32.7767, -96.7970),
    'atlanta': (33.7490, -84.3880),
    'boston': (42.3601, -71.0589),
    'washington dc': (38.9072, -77.0369),
    'dc': (38.9072, -77.0369),
    # Europe
    'london': (51.5074, -0.1278),
    'paris': (48.8566, 2.3522),
    'berlin': (52.5200, 13.4050),
    'amsterdam': (52.3676, 4.9041),
    'rome': (41.9028, 12.4964),
    'madrid': (40.4168, -3.7038),
    'vienna': (48.2082, 16.3738),
    'zurich': (47.3769, 8.5417),
    'moscow': (55.7558, 37.6173),
    # Middle East
    'dubai': (25.2048, 55.2708),
    'riyadh': (24.7136, 46.6753),
    # Oceania
    'sydney': (-33.8688, 151.2093),
    'melbourne': (-37.8136, 144.9631),
    # South America
    'sao paulo': (-23.5505, -46.6333),
    'buenos aires': (-34.6037, -58.3816),
}


def get_city_coords(city_name: str) -> Optional[Tuple[float, float]]:
    """Look up coordinates for a city — AIRPORT FIRST (Polymarket resolution station),
    fall back to city center, then to CITY_COORDS database."""
    key = city_name.lower().strip()

    # 1. Check weather_stations for EXACT airport coordinates (THE EDGE)
    from data.weather_stations import get_airport_coords
    airport = get_airport_coords(key)
    if airport:
        return airport

    # 2. Fall back to city center coordinates
    return CITY_COORDS.get(key)
