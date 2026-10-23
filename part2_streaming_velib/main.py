from __future__ import annotations
import argparse
import gzip
import io
import json
import logging
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from threading import Lock

import pandas as pd
import requests
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from urbanhub_common.ops import start_run, finish_run
from urbanhub_common import write_parquet
from urbanhub_common.lake import read_parquet_dataset


load_dotenv()
LOG = logging.getLogger('urbanhub.citybikes')
API = 'https://api.citybik.es/v2'
DEFAULT_CITIES = ['Paris', 'Lyon', 'Marseille', 'Toulouse', 'Bordeaux', 'Nantes', 'Lille', 'Strasbourg', 'Rennes', 'Montpellier']
COLUMNS = ['station_id', 'longitude', 'free_slots', 'station_name', 'timestamp', 'latitude', 'bikes_available', 'network_id', 'city', 'collected_at']
_API_LOCK = Lock()
_API_COOLDOWN_UNTIL = 0.0
_API_CALLS = 0


class ApiRateLimited(RuntimeError):
    @property
    def retry_after(self):
        with _API_LOCK:
            return max(1, _API_COOLDOWN_UNTIL - time.monotonic())


def rate_limit_delay(headers):
    """Retry-After may be seconds or an HTTP date; RateLimit-Reset is seconds."""
    value = headers.get('Retry-After')
    if value:
        try:
            return max(1, int(value))
        except ValueError:
            try:
                return max(1, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    try:
        return max(1, int(headers['RateLimit-Reset']))
    except (KeyError, ValueError, TypeError):
        return 3600


def restore_s3_error(http_response, parsed, **kwargs):
    """Recover S3 errors when the server labels its XML body as text/plain."""
    if http_response.status_code < 400 or parsed.get('Error', {}).get('Code') not in (None, '', '400'):
        return
    try:
        root = ET.fromstring(http_response.content)
    except ET.ParseError:
        return
    if root.tag.rsplit('}', 1)[-1] != 'Error':
        return
    fields = {child.tag.rsplit('}', 1)[-1]: child.text for child in root}
    if fields.get('Code'):
        parsed['Error'] = {'Code': fields['Code'], 'Message': fields.get('Message') or ''}


def s3_client():
    from urbanhub_common import s3_client as shared_s3_client
    return shared_s3_client()


def bucket():
    from urbanhub_common import lake_bucket
    return lake_bucket()


def api_get(path):
    global _API_COOLDOWN_UNTIL, _API_CALLS
    with _API_LOCK:
        if time.monotonic() < _API_COOLDOWN_UNTIL:
            raise ApiRateLimited('CityBikes API cooldown is active')
        _API_CALLS += 1
    r = requests.get(API + path, timeout=30, headers={'User-Agent':'UrbanHub-student-project/1.0'})
    if r.status_code == 429:
        delay = rate_limit_delay(r.headers)
        with _API_LOCK:
            _API_COOLDOWN_UNTIL = max(_API_COOLDOWN_UNTIL, time.monotonic() + delay)
        raise ApiRateLimited('CityBikes API hourly request limit reached')
    r.raise_for_status()
    return r.json()


@lru_cache(maxsize=1)
def network_directory():
    # Network IDs rarely change; reuse the directory until this process exits.
    return api_get('/networks').get('networks', [])


def list_networks(cities):
    city_lookup = {c.strip().casefold(): c.strip() for c in cities}
    result = []
    for network in network_directory():
        loc = network.get('location') or {}
        country = str(loc.get('country', '')).upper()
        city = str(loc.get('city', '')).strip()
        # Paris often has multiple provider networks. Select all matching French networks.
        if country in ('FR', 'FRA') and city.casefold() in city_lookup:
            result.append((network['id'], city_lookup[city.casefold()]))
    return result


def snapshot(network_id, city, collected_at):
    obj = api_get('/networks/' + network_id)
    stations = (obj.get('network') or {}).get('stations') or []
    records = []
    for st in stations:
        records.append({
            'station_id': str(st.get('id') or ''),
            'longitude': st.get('longitude'),
            'free_slots': st.get('empty_slots'),
            'station_name': st.get('name'),
            'timestamp': st.get('timestamp'),
            'latitude': st.get('latitude'),
            'bikes_available': st.get('free_bikes'),
            'network_id': network_id,
            'city': city,
            'collected_at': collected_at,
        })
    return obj, records


def parquet_bytes(frame):
    buf = io.BytesIO()
    frame.to_parquet(buf, engine='pyarrow', index=False, compression='zstd')
    return buf.getvalue()


def upload(client, key, data, content_type):
    client.put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)


def parse_station_timestamps(values):
    # CityBikes sometimes appends Z to an already explicit UTC offset.
    normalized = values.astype('string').str.replace(r'\+00:00Z$', 'Z', regex=True)
    return pd.to_datetime(normalized, format='ISO8601', errors='coerce', utc=True)


def collect_one(client, network_id, city, collected_at):
    try:
        raw, records = snapshot(network_id, city, collected_at)
        dt = datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        partition = f'city={city.lower()}/network={network_id}/date={dt:%Y-%m-%d}/hour={dt:%H}'
        file_id = dt.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        bronze_key = f'bronze/citybikes/{partition}/{file_id}.json.gz'
        silver_key = f'silver/citybikes/{partition}/{file_id}.parquet'
        upload(client, bronze_key, gzip.compress(json.dumps(raw, ensure_ascii=False).encode('utf-8')), 'application/gzip')
        if not records:
            LOG.warning('No stations for %s', network_id)
            return 0
        frame = pd.DataFrame.from_records(records, columns=COLUMNS)
        for col in ('longitude', 'latitude', 'free_slots', 'bikes_available'):
            frame[col] = pd.to_numeric(frame[col], errors='coerce')
        frame = frame.dropna(subset=['station_id', 'latitude', 'longitude'])
        frame = frame[(frame.station_id != '') & frame.latitude.between(-90, 90) & frame.longitude.between(-180, 180)]
        for col in ('free_slots', 'bikes_available'):
            frame.loc[frame[col] < 0, col] = float('nan')
        frame['timestamp'] = parse_station_timestamps(frame['timestamp'])
        frame['collected_at'] = pd.to_datetime(frame['collected_at'], utc=True)
        frame = frame.drop_duplicates(subset=['network_id', 'station_id'], keep='last')
        if frame.empty:
            LOG.warning('All stations filtered for %s', network_id)
            return 0
        committed = write_parquet(frame, 'silver', 'citybikes', 'collected_at')
        LOG.info('%s: %s stations -> Iceberg silver.citybikes (%s)', city, len(frame), committed[0])
        return len(frame)
    except ApiRateLimited:
        raise
    except Exception:
        LOG.exception('Collection failed for network=%s city=%s', network_id, city)
        return 0


def collect(client, cities, workers=4):
    """One logged polling cycle; data and run events are persisted in MinIO."""
    run_id = start_run('mobility', 'citybikes-poll')
    with _API_LOCK:
        calls_before = _API_CALLS
    total = 0
    failed = 0
    try:
        networks = list_networks(cities)
        if not networks:
            raise RuntimeError('No matching CityBikes networks for the selected cities')
        LOG.info('Found %d networks: %s', len(networks), networks)
        at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        rate_error = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(collect_one, client, nid, city, at) for nid, city in networks]
            # Drain every worker so partial writes and API-call counts are accounted for.
            for future in as_completed(futures):
                try:
                    rows = future.result()
                    total += rows
                    failed += rows == 0
                except ApiRateLimited as exc:
                    failed += 1
                    rate_error = exc
        if rate_error is not None:
            raise rate_error
    except Exception as exc:
        with _API_LOCK:
            calls = _API_CALLS - calls_before
        finish_run(run_id, 'error', api_calls=calls, rows_inserted=total,
                   raw_path=f's3://{bucket()}/bronze/citybikes/', error=str(exc)[:2000])
        raise
    with _API_LOCK:
        calls = _API_CALLS - calls_before
    status = 'success' if failed == 0 else ('partial' if total else 'error')
    finish_run(run_id, status, api_calls=calls, rows_inserted=total,
               raw_path=f's3://{bucket()}/bronze/citybikes/',
               error=f'{failed}/{len(networks)} networks produced no valid stored observations' if failed else None)
    return total


def iter_keys(client, prefix):
    pager = client.get_paginator('list_objects_v2')
    for page in pager.paginate(Bucket=bucket(), Prefix=prefix):
        for obj in page.get('Contents', []):
            yield obj['Key']


def fetch_history(client, days, cities, max_files):
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=days)
    result=read_parquet_dataset('citybikes',cutoff,pd.Timestamp.now(tz='UTC'))
    if result.empty:
        return pd.DataFrame(columns=COLUMNS)
    result=result[result.city.isin(cities)].copy()
    result['collected_at']=pd.to_datetime(result['collected_at'],utc=True,errors='coerce')
    result['timestamp']=parse_station_timestamps(result['timestamp'])
    return result.drop_duplicates(subset=['network_id','station_id','collected_at'])


def analyze(client, days, cities, max_files):
    df = fetch_history(client, days, cities, max_files)
    if df.empty:
        print('No stored snapshots in the selected period. Run --once first.')
        return
    for col in ('bikes_available', 'free_slots'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['capacity_observed'] = df.bikes_available + df.free_slots
    df = df[(df.capacity_observed > 0) & (df.bikes_available >= 0) & (df.free_slots >= 0)].copy()
    if df.empty:
        print('No valid station capacity observations.')
        return
    df['availability_ratio'] = df.bikes_available / df.capacity_observed
    df['occupancy_ratio'] = 1 - df.availability_ratio
    df['low_supply'] = df.availability_ratio <= 0.10
    df['full_station'] = df.free_slots == 0
    df['hour_utc'] = df.collected_at.dt.hour
    df['hour_france'] = df.collected_at.dt.tz_convert('Europe/Paris').dt.hour
    group = ['city', 'network_id', 'station_id', 'station_name']
    stations = df.groupby(group, dropna=False).agg(
        observations=('station_id', 'size'), average_occupancy=('occupancy_ratio','mean'),
        low_supply_pct=('low_supply','mean'), latitude=('latitude','first'), longitude=('longitude','first'),
        avg_bikes=('bikes_available','mean')).reset_index()
    q1 = stations.sort_values('average_occupancy', ascending=False)
    # Approximate geographic areas using 0.01-degree bins (~1km latitude).
    df['geo_lat_bin'] = (df.latitude / 0.01).round() * 0.01
    df['geo_lon_bin'] = (df.longitude / 0.01).round() * 0.01
    q2 = df.groupby(['city','geo_lat_bin','geo_lon_bin']).agg(
        observations=('station_id','size'), low_supply_pct=('low_supply','mean'),
        average_bikes=('bikes_available','mean')).reset_index().sort_values('low_supply_pct', ascending=False)
    q3 = df.groupby(['city','hour_france']).agg(
        observations=('station_id','size'), low_supply_pct=('low_supply','mean'),
        average_occupancy=('occupancy_ratio','mean')).reset_index().sort_values(['city','hour_france'])
    q4 = df.groupby(['city','geo_lat_bin','geo_lon_bin']).agg(
        observations=('station_id','size'), average_availability=('availability_ratio','mean'),
        empty_station_pct=('low_supply','mean'), full_station_pct=('full_station','mean')).reset_index().sort_values(['city','average_availability'])
    # Consecutive low-supply snapshots per station; require 3 observations within roughly 10 minutes.
    ordered = df.sort_values(['network_id','station_id','collected_at']).copy()
    ordered['low_int'] = ordered.low_supply.astype(int)
    key = [ordered[c] for c in ['network_id','station_id']]
    ordered['three_low'] = (ordered.groupby(['network_id','station_id'])['low_int'].transform(lambda s: s.rolling(3, min_periods=3).sum()) == 3)
    ordered['lag2_time'] = ordered.groupby(['network_id','station_id'])['collected_at'].shift(2)
    ordered['three_low'] &= ((ordered.collected_at - ordered.lag2_time) <= pd.Timedelta(minutes=10))
    q5 = ordered.groupby(group, dropna=False).agg(
        critical_events=('three_low','sum'), low_supply_pct=('low_supply','mean'),
        observations=('station_id','size')).reset_index().query('critical_events > 0').sort_values('critical_events', ascending=False)
    outputs = {'q1_top_occupied_stations':q1, 'q2_undersupplied_areas':q2,
        'q3_hourly_patterns_france_time':q3, 'q4_geographic_imbalance':q4,
        'q5_critical_stations':q5}
    now = datetime.now(timezone.utc)
    stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
    for label, frame in outputs.items():
        key = f'gold/citybikes/run={stamp}/{label}.csv'
        write_parquet(frame.assign(computed_at=now), 'gold', 'citybikes/'+label, 'computed_at')
        upload(client, key, frame.to_csv(index=False).encode('utf-8'), 'text/csv')
        print(f'{label}: {len(frame)} rows -> {key}')
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5))
        for city, chunk in q3.groupby('city'):
            ax.plot(chunk.hour_france, 100*chunk.low_supply_pct, label=city)
        ax.set(xlabel='Hour of day (Europe/Paris)', ylabel='Stations with <=10% bikes (%)', title='UrbanHub: daily bike shortages')
        ax.legend(fontsize=8, ncol=2)
        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format='png', dpi=140)
        plt.close(fig)
        key=f'gold/citybikes/run={stamp}/hourly_shortages.png'
        upload(client,key,out.getvalue(),'image/png')
        print('Chart ->',key)
    except ImportError:
        LOG.warning('matplotlib not installed; skipping chart')
    print('Important: occupancy is a stock indicator, NOT actual trips or utilization turnover.')


def main():
    parser=argparse.ArgumentParser(description='UrbanHub CityBikes -> MinIO Data Lake')
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check-s3', action='store_true')
    group.add_argument('--once', action='store_true')
    group.add_argument('--interval', type=int, metavar='SECONDS')
    group.add_argument('--analyze', action='store_true')
    p=parser.add_argument('--cities', nargs='+', default=DEFAULT_CITIES)
    parser.add_argument('--days', type=int, default=2)
    parser.add_argument('--max-files', type=int, default=2000)
    parser.add_argument('--workers', type=int, default=4)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.interval is not None and args.interval < 120:
        parser.error('--interval must be at least 120 seconds')
    client=s3_client()
    if args.check_s3:
        try:
            # GET returns an error body, unlike HEAD, and verifies listing access.
            client.list_objects_v2(Bucket=bucket(), MaxKeys=1)
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code', 'Unknown')
            if code in ('InvalidSignature', 'InvalidAccessKeyId', 'SignatureDoesNotMatch'):
                parser.exit(1, f'S3 authentication failed ({code}). Check S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY against the MinIO credentials in .env.\n')
            parser.exit(1, f'S3 bucket check failed ({code}). Verify the bucket exists and the S3 credentials have access.\n')
        except BotoCoreError:
            parser.exit(1, 'Could not complete the S3 request. Verify the endpoint, network connection and client configuration.\n')
        print(f'Connected to MinIO S3 bucket: {bucket()}')
    elif args.once:
        try:
            print(f'Collected {collect(client,args.cities,args.workers)} station observations')
        except ApiRateLimited as exc:
            parser.exit(1, f'CityBikes rate limit reached. Retry after {exc.retry_after:.0f} seconds.\n')
    elif args.analyze:
        analyze(client,args.days,args.cities,args.max_files)
    else:
        print(f'Collecting every {args.interval}s. Ctrl+C to stop.')
        try:
            while True:
                started=time.monotonic()
                cooldown=0
                try:
                    print(f'Collected {collect(client,args.cities,args.workers)} station observations')
                except ApiRateLimited as exc:
                    cooldown=exc.retry_after
                    LOG.warning('CityBikes rate limit reached; collection will retry in %.0f seconds. Some networks may already have been uploaded.', max(cooldown, args.interval-(time.monotonic()-started)))
                except Exception:
                    LOG.exception('Collection cycle failed')
                time.sleep(max(cooldown, 0, args.interval-(time.monotonic()-started)))
        except KeyboardInterrupt:
            print('Stopped.')

if __name__=='__main__':
    main()
