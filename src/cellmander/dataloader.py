# vtdspatial/dataloader.py
import os
import re
import requests
from io import BytesIO, StringIO
import pandas as pd
from tqdm import tqdm

ALARM_RAW_BASE = "https://raw.githubusercontent.com/alarm-redist/census-2020/main/census-vest-2020"

# some states use block-level files (CA, HI, OR in ALARM); fallback logic handles that
def alarm_csv_url_for(state_abbr: str):
    state_abbr = state_abbr.lower()
    candidates = [
        f"{ALARM_RAW_BASE}/{state_abbr}_2020_vtd.csv",
        f"{ALARM_RAW_BASE}/{state_abbr}_2020_block.csv"
    ]
    for url in candidates:
        r = requests.head(url)
        if r.status_code == 200:
            return url
    raise FileNotFoundError(f"ALARM CSV not found for {state_abbr} (tried vtd/block).")

def download_alarm_csv(state_abbr: str, dest_dir="data"):
    """Download ALARM CSV for one state; return path to local CSV file."""
    os.makedirs(dest_dir, exist_ok=True)
    url = alarm_csv_url_for(state_abbr)
    local_path = os.path.join(dest_dir, os.path.basename(url))
    if os.path.exists(local_path):
        return local_path
    r = requests.get(url, stream=True)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_path

def read_alarm_csv(csv_path_or_buffer):
    """Load a CSV into pandas DataFrame (ALARM CSV)."""
    return pd.read_csv(csv_path_or_buffer, dtype=str, low_memory=False)

def detect_and_extract_2020_vote_cols(df: pd.DataFrame):
    """
    Return (dem_col, rep_col) to use for 2020 presidential vote totals.
    Strategy:
      1) prefer explicit 'pre_2020_dem' and 'pre_2020_rep' if present.
      2) else find all columns that match 'pre_2020_.*_dem' / 'pre_2020_.*_rep' and sum them.
      3) else fallback to 'pre_2020_dem' style not found -> try candidate-specific columns OR use 'ndv'/'nrv' (avg) as last resort.
    """
    cols = set(df.columns.str.lower())
    # exact names (common)
    if 'pre_2020_dem' in cols and 'pre_2020_rep' in cols:
        return 'pre_2020_dem', 'pre_2020_rep'
    # pattern: pre_2020_<office>_<party>_<can> or pre_2020_<can>_dem  -- collect any columns containing 'pre_2020' and ending with _dem/_rep
    dem_patterns = [c for c in df.columns if re.match(r"(?i).*pre[_\-]?2020.*_dem$", c)]
    rep_patterns = [c for c in df.columns if re.match(r"(?i).*pre[_\-]?2020.*_rep$", c)]
    if dem_patterns and rep_patterns:
        return dem_patterns, rep_patterns  # note: list => caller should sum columns
    # fallback: specific candidate names might exist, e.g., pre_2020_bid or pre_2020_tru but party suffix usually present
    # Last resort: use ndv/nrv (averaged votes across elections) if present
    if 'ndv' in cols and 'nrv' in cols:
        return 'ndv', 'nrv'
    # nothing found
    raise KeyError("Couldn't find 2020 Dem/Rep vote columns. Inspect columns: " + ", ".join(df.columns[:50]))

def extract_vote_totals(df: pd.DataFrame):
    """
    Return df with columns:
      state_id, county_id, voting_district (VTD), GEOID20, n_vote_dem_2020, n_vote_rep_2020
    """
    # keep original column names (case sensitive) but we will inspect lowercases
    # canonical id columns:
    # - GEOID20 (preferred)
    # - state, county columns also in ALARM CSV (they might be numeric strings)
    colmap = {c.lower(): c for c in df.columns}
    if 'geoid20' in colmap:
        geoid_col = colmap['geoid20']
    else:
        # try to compose from STATEFP+COUNTYFP+VTD if available
        geoid_col = None

    # detect votes
    dem_col_spec, rep_col_spec = detect_and_extract_2020_vote_cols(df)

    # compute dem/rep totals robustly (handle list-of-columns vs single)
    def sum_cols(spec):
        if isinstance(spec, (list, tuple)):
            out = df[spec].astype(float).sum(axis=1)
        else:
            out = df[spec].astype(float)
        return out

    df_out = pd.DataFrame()
    # state fips or code columns
    if 'state' in colmap:
        df_out['state_id'] = df[colmap['state']].astype(str)
    if 'county' in colmap:
        df_out['county_id'] = df[colmap['county']].astype(str)
    # voting district id: ALARM uses VTD field or GEOID20 - try VTD if present:
    if 'vtd' in colmap:
        df_out['voting_district'] = df[colmap['vtd']].astype(str)
    elif geoid_col is not None:
        # fallback: use last 5-6 digits of GEOID20 as local VTD if no VTD field
        df_out['voting_district'] = df[geoid_col].astype(str)
    else:
        df_out['voting_district'] = None

    if geoid_col is not None:
        df_out['GEOID20'] = df[geoid_col].astype(str)

    df_out['n_vote_dem_2020'] = sum_cols(dem_col_spec)
    df_out['n_vote_rep_2020'] = sum_cols(rep_col_spec)

    return df_out
