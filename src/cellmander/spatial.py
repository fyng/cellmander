import os
import zipfile
import requests
import geopandas as gpd
import pandas as pd

# small helper table: state abbr -> fips (string zero-padded) and name for census path
STATE_FIPS = {
    'al': ('01','Alabama'), 'ak':('02','Alaska'), 'az':('04','Arizona'),
    'ar':('05','Arkansas'), 'ca':('06','California'), 'co':('08','Colorado'),
    'ct':('09','Connecticut'), 'de':('10','Delaware'), 'dc':('11','District_of_Columbia'),
    'fl':('12','Florida'), 'ga':('13','Georgia'), 'hi':('15','Hawaii'),
    'id':('16','Idaho'), 'il':('17','Illinois'), 'in':('18','Indiana'),
    'ia':('19','Iowa'), 'ks':('20','Kansas'), 'ky':('21','Kentucky'),
    'la':('22','Louisiana'), 'me':('23','Maine'), 'md':('24','Maryland'),
    'ma':('25','Massachusetts'), 'mi':('26','Michigan'), 'mn':('27','Minnesota'),
    'ms':('28','Mississippi'), 'mo':('29','Missouri'), 'mt':('30','Montana'),
    'ne':('31','Nebraska'), 'nv':('32','Nevada'), 'nh':('33','New_Hampshire'),
    'nj':('34','New_Jersey'), 'nm':('35','New_Mexico'), 'ny':('36','New_York'),
    'nc':('37','North_Carolina'), 'nd':('38','North_Dakota'), 'oh':('39','Ohio'),
    'ok':('40','Oklahoma'), 'or':('41','Oregon'), 'pa':('42','Pennsylvania'),
    'ri':('44','Rhode_Island'), 'sc':('45','South_Carolina'), 'sd':('46','South_Dakota'),
    'tn':('47','Tennessee'), 'tx':('48','Texas'), 'ut':('49','Utah'),
    'vt':('50','Vermont'), 'va':('51','Virginia'), 'wa':('53','Washington'),
    'wv':('54','West_Virginia'), 'wi':('55','Wisconsin'), 'wy':('56','Wyoming')
}

CENSUS_TIGER_BASE_PL = "https://www2.census.gov/geo/tiger/TIGER2020PL"

def tiger_vtd_url(state_abbr: str):
    """Construct the Census PL-state folder URL for the state's VTD zip if possible."""
    state_abbr = state_abbr.lower()
    fips, state_name = STATE_FIPS[state_abbr]
    # path observed on census site: /TIGER2020PL/STATE/{fips}_{STATE_NAME}/tl_2020_{fips}_vtd20.zip
    zip_name = f"tl_2020_{fips}_vtd20.zip"
    url = f"{CENSUS_TIGER_BASE_PL}/LAYER/VTD/2020/{zip_name}"
    return url


def download_tiger_vtd(state_abbr: str, dest_dir="data/shapefiles"):
    os.makedirs(dest_dir, exist_ok=True)
    url = tiger_vtd_url(state_abbr)
    local_zip = os.path.join(dest_dir, os.path.basename(url))
    if os.path.exists(local_zip):
        return local_zip
    r = requests.get(url, stream=True, verify=False)
    r.raise_for_status()
    with open(local_zip, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_zip


def read_vtd_shapefile(zip_path):
    """
    Read the VTD shapefile from the zip and return a GeoDataFrame.
    geopandas can read zipped shapefiles directly with a 'zip://' or via fiona by passing the path.
    We'll extract to a temp dir for simplicity.
    """
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(tmp)
    # find the shapefile name
    shp_files = [f for f in os.listdir(tmp) if f.endswith('.shp')]
    if not shp_files:
        shutil.rmtree(tmp)
        raise FileNotFoundError("No .shp found inside zip")
    shp_path = os.path.join(tmp, shp_files[0])
    gdf = gpd.read_file(shp_path)
    # cleanup left to caller or OS; keep in memory
    return gdf

def compute_centroids_and_join(alarm_df, vtd_gdf, keep_geometry=False):
    """
    alarm_df: pandas DataFrame with GEOID20 (or voting id)
    vtd_gdf: GeoDataFrame read from TIGER VTD shapefile
    Returns: joined GeoDataFrame with columns:
      state_id, county_id, voting_district, GEOID20, n_vote_dem_2020, n_vote_rep_2020, x, y
    - centroids are computed after projecting to EPSG:5070 (US National Albers)
    """
    # unify possible geoid column names in vtd_gdf
    vtd_cols_lc = {c.lower(): c for c in vtd_gdf.columns}
    if 'geoid20' in vtd_cols_lc:
        vtd_geoid_col = vtd_cols_lc['geoid20']
    elif 'geoid' in vtd_cols_lc:
        vtd_geoid_col = vtd_cols_lc['geoid']
    else:
        # try to construct GEOID20 from STATEFP + COUNTYFP + VTD (common)
        sf = vtd_cols_lc.get('statefp', None)
        cf = vtd_cols_lc.get('countyfp', None)
        vtdcol = vtd_cols_lc.get('vtd', None) or vtd_cols_lc.get('vtdce', None)
        if sf and cf and vtdcol:
            vtd_gdf['GEOID20'] = (vtd_gdf[sf].astype(str).str.zfill(2) +
                                  vtd_gdf[cf].astype(str).str.zfill(3) +
                                  vtd_gdf[vtdcol].astype(str))
            vtd_geoid_col = 'GEOID20'
        else:
            raise KeyError("Cannot find GEOID/STATEFP/COUNTYFP/VTD fields in VTD shapefile.")
    # ensure same dtype
    vtd_gdf[vtd_geoid_col] = vtd_gdf[vtd_geoid_col].astype(str)
    if 'GEOID20' in alarm_df.columns:
        join_key = 'GEOID20'
        alarm_df['GEOID20'] = alarm_df['GEOID20'].astype(str)
    else:
        # fallback: maybe the alarm dataframe's 'voting_district' and state/county exist
        # user will need to provide a join key; we attempt to join on state+county+voting_district
        if {'state_id','county_id','voting_district'}.issubset(alarm_df.columns):
            alarm_df['join_key'] = (alarm_df['state_id'].str.zfill(2) +
                                    alarm_df['county_id'].str.zfill(3) +
                                    alarm_df['voting_district'].astype(str))
            join_key = 'join_key'
            # make vtd geoid column name match
            if vtd_geoid_col != 'GEOID20':
                vtd_gdf['GEOID20'] = vtd_gdf[vtd_geoid_col]
                vtd_geoid_col = 'GEOID20'
        else:
            raise KeyError("alarm_df missing GEOID20 and state+county+voting_district to assemble join key.")

    # project to Albers (EPSG:5070) for centroid calculation (units in meters, good for continental US)
    vtd_proj = vtd_gdf.to_crs(epsg=5070)
    vtd_proj['centroid'] = vtd_proj.geometry.centroid
    vtd_proj['x'] = vtd_proj.centroid.x
    vtd_proj['y'] = vtd_proj.centroid.y

    # prepare dataframe for join: keep geoid and x/y
    vtd_xy = vtd_proj[[vtd_geoid_col, 'x', 'y', 'geometry']].rename(columns={vtd_geoid_col:'GEOID20'})
    # merge alarm df with vtd_xy on 'GEOID20' or join_key (ensure alarm_df has GEOID20 in that case)
    if join_key == 'GEOID20':
        merged = alarm_df.merge(vtd_xy.drop(columns=['geometry']), on='GEOID20', how='left')
    else:
        # alarm has join_key constructed and vtd has GEOID20 as geoid; map vtd GEOID20 to join_key-like values
        # We already set vtd_proj['GEOID20'] available; assume alarm join_key equals vtd GEOID20
        merged = alarm_df.merge(vtd_xy.drop(columns=['geometry']).rename(columns={'GEOID20':'GEOID20'}), left_on=join_key, right_on='GEOID20', how='left')

    if keep_geometry:
        # bring geometry too (in projected CRS)
        merged = merged.merge(vtd_proj[[vtd_geoid_col, 'geometry']].rename(columns={vtd_geoid_col:'GEOID20'}), on='GEOID20', how='left')
        gdf = gpd.GeoDataFrame(merged, geometry='geometry', crs='EPSG:5070')
        return gdf
    else:
        return merged