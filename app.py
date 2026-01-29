import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely import wkt
import googlemaps
import pydeck as pdk
import numpy as np
from sklearn.neighbors import BallTree
import re

st.set_page_config(page_title="NYC Sports Navigator", page_icon="🍎", layout="wide")

@st.cache_data
def load_and_prep_data():
    try:
        df_fac = pd.read_csv("data/Athletic_Facilities.csv")
        df_prop = pd.read_csv("data/Parks_Properties.csv")
    except FileNotFoundError:
        st.error("CSV files not found."); return gpd.GeoDataFrame(), []

    # 1. Merge & Basic Filter
    df = pd.merge(df_fac, df_prop, on="GISPROPNUM", how="left", suffixes=('_fac', '_prop'))
    if 'FEATURESTATUS' in df.columns: df = df[df['FEATURESTATUS'] == 'Active'].copy()

    # 2. Unify Columns
    def coalesce(df, columns, new_col):
        df[new_col] = np.nan
        for c in columns:
            if c in df.columns: df[new_col] = df[new_col].fillna(df[c])
        return df

    df = coalesce(df, ['address', 'ADDRESS', 'ADDRESS_fac', 'ADDRESS_prop', 'Location'], 'address')
    df = coalesce(df, ['borough', 'BOROUGH', 'BOROUGH_fac', 'BOROUGH_prop'], 'borough')
    df = coalesce(df, ['SIGNNAME', 'SIGNNAME_prop', 'NAME311', 'NAME311_fac'], 'raw_name')
    
    df['address'] = df['address'].fillna("Address not listed")
    df['borough'] = df['borough'].fillna("Unknown")
    df['raw_name'] = df['raw_name'].fillna("Unnamed Facility")

    # 3. Clean Names (Robust Logic)
    def clean_name(val):
        val_str = str(val).strip()
        # Force all BBP variations to one canonical name
        if "BROOKLYN BRIDGE PARK" in val_str.upper():
            return "Brooklyn Bridge Park"
        
        # Standard cleaning for others
        val_str = re.sub(r'\s+\d+$', '', val_str) 
        val_str = re.sub(r'\s+(Tennis|Basketball|Handball)?\s*(Court|Field|Playground)\s*\d*$', '', val_str, flags=re.I)
        return val_str.strip()
        
    df['name'] = df['raw_name'].apply(clean_name)

    # 4. Geometry Parsing
    geo_col = next((c for c in ['multipolygon', 'multipolygon_fac', 'geometry'] if c in df.columns), None)
    if not geo_col: return gpd.GeoDataFrame(), []
    
    df = df.dropna(subset=[geo_col]).copy()
    try:
        df['geometry'] = df[geo_col].astype(str).apply(wkt.loads)
        gdf = gpd.GeoDataFrame(df, geometry='geometry')
        gdf['lat_raw'] = gdf.geometry.representative_point().y
        gdf['lon_raw'] = gdf.geometry.representative_point().x
    except: return gpd.GeoDataFrame(), []

    # 5. Identify Sport Columns (Strict Filter)
    blocklist = ['ACCESSIBLE','LIGHTED','PERMIT','RETIRED','WATERFRONT','MAPPED','PIP','PIP_RATABLE','AGREEMENT',
                 'BOARD','DISTRICT','PRECINCT','ZIP','LAT','LON','ID','OBJ','PARENT','ACRES','COORD','SHAPE',
                 'SYSTEM','DEPT','JURIS','CAT','CLASS','SIGN','LOC','POLY','STATUS','DIM','GISPROPNUM','NAME',
                 'ADDRESS','BOROUGH','MULTIPOLYGON','RAW_NAME','LAT_RAW','LON_RAW']
    
    sport_cols = [c for c in gdf.columns if c.upper() not in blocklist 
                  and not any(b in c.upper() for b in blocklist)
                  and len(gdf[c].unique()) <= 5 
                  and any(x in str(gdf[c].unique()).lower() for x in ['yes','true','1'])]

    # 6. Aggregation
    agg_dict = {'lat_raw': 'mean', 'lon_raw': 'mean', 'address': 'first', 'borough': 'first'}
    for s in sport_cols: agg_dict[s] = 'max'
    
    grouped = gdf.groupby('name', as_index=False).agg(agg_dict)
    grouped = grouped.rename(columns={'lat_raw': 'latitude', 'lon_raw': 'longitude'})

    # 7. MANUAL FIX: Brooklyn Bridge Park
    target_idx = grouped[grouped['address'] == "128 PROSPECT STREET"].index
    if not target_idx.empty:
        grouped.loc[target_idx, 'latitude'] = 40.69958403955078
        grouped.loc[target_idx, 'longitude'] = -73.99866435437906
        grouped.loc[target_idx, 'name'] = "Brooklyn Bridge Park"

    return grouped, sorted(sport_cols)

# --- SEARCH LOGIC ---
def get_nearest(lat, lon, df, k=5):
    if df.empty: return pd.DataFrame()
    
    coords = np.deg2rad(df[['latitude', 'longitude']].values)
    tree = BallTree(coords, metric='haversine')
    dists, idxs = tree.query(np.deg2rad([[lat, lon]]), k=min(len(df), k*2))
    
    res = df.iloc[idxs[0]].copy()
    res['dist_miles'] = dists[0] * 3958.8
    return res.head(k)

# --- MAP RENDERER ---
def render_map(u_lat, u_lon, df):
    df = df.copy()
    df['dist_txt'] = df['dist_miles'].apply(lambda x: f"{x:.2f} miles")
    
    # 1. Prepare Ring Data (Circles + Labels)
    rings_data = []
    text_data = []
    distances = [0.25, 0.5, 0.75, 1, 2, 3, 4, 5, 10]
    
    # Pre-calculate conversion factor for longitude (Meters -> Degrees) based on user latitude
    # 1 degree longitude = 111,320 meters * cos(latitude)
    meters_per_deg_lon = 111320 * np.cos(np.deg2rad(u_lat))
    
    for m in distances:
        r_meters = m * 1609.34
        rings_data.append({"lat": u_lat, "lon": u_lon, "r": r_meters})
        
        if m >= 1:
            label_lon = u_lon + (r_meters / meters_per_deg_lon)
            text_data.append({
                "pos": [label_lon, u_lat],
                "text": f"{m} mi"
            })

    # 2. Define Layers
    layers = [
        # Rings Layer
        pdk.Layer("ScatterplotLayer", data=rings_data,
                  get_position=["lon", "lat"], get_radius="r", stroked=True, filled=False, 
                  get_line_color=[100,100,100,80], line_width_min_pixels=0.5),
        
        # Text Labels Layer (New)
        pdk.Layer("TextLayer", data=text_data,
                  get_position="pos", get_text="text", get_size=11, get_color=[80, 80, 80],
                  get_alignment_baseline="'center'", get_text_anchor="'start'", # Start = Left-align text to the point
                  pixel_offset=[5, 0]), # Slight padding to right
        
        # Facilities Layer
        pdk.Layer("ScatterplotLayer", data=df, get_position=["longitude", "latitude"],
                  get_color=[255, 69, 0], get_line_color=[255, 255, 255], stroked=True, 
                  line_width_min_pixels=3, radius_min_pixels=8, radius_max_pixels=25, pickable=True),
        
        # User Location Layer
        pdk.Layer("ScatterplotLayer", data=[{'lat': u_lat, 'lon': u_lon, 'name': 'You', 'address': 'Start', 'dist_txt': '0 mi'}],
                  get_position=["lon", "lat"], get_color=[30, 144, 255], get_line_color=[255, 255, 255], 
                  stroked=True, line_width_min_pixels=3, radius_min_pixels=8, radius_max_pixels=25, pickable=True)
    ]

    # 3. View Logic (Zoom Control)
    # view_proportion=0.9 ensures the data points take up 90% of the screen, creating a nice tight fit
    view_pts = [[u_lon, u_lat]] + df[['longitude', 'latitude']].values.tolist()
    view = pdk.data_utils.compute_view(view_pts, view_proportion=0.9)
    view.pitch, view.bearing = 0, 0

    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view, map_style="light", 
                             tooltip={"html": "<b>{name}</b><br/>{address}<br/>{dist_txt}", 
                                      "style": {"color": "black", "backgroundColor": "white"}}), 
                                      height=820)

# --- MAIN APP ---
def main():
    st.title("🍎 NYC Sports Navigator")
    with st.sidebar:
        api_key = st.text_input("Google Maps API Key", type="password")

    gdf, sports = load_and_prep_data()
    if gdf.empty: st.stop()

    c1, c2 = st.columns([1, 2])
    with c1: sport = st.selectbox("I want to play:", sports, index=sports.index('BASKETBALL') if 'BASKETBALL' in sports else 0)
    with c2: addr = st.text_input("Near:", "10001")

    if st.button("🔍 Find", type="primary"):
        if not api_key: st.error("API Key required."); return
        try:
            gmaps = googlemaps.Client(key=api_key)
            geo = gmaps.geocode(f"{addr}, New York, NY")
            if not geo: st.error("Location not found."); return
            lat, lon = geo[0]['geometry']['location'].values()
            st.success(f"📍 {geo[0]['formatted_address']}")
        except Exception as e: st.error(f"Error: {e}"); return

        # Filter
        active = gdf[gdf[sport].astype(str).str.lower().isin(['1','true','yes','y'])].copy()
        if active.empty: st.warning("No facilities found."); return
        
        results = get_nearest(lat, lon, active)

        # Output
        cm, cl = st.columns([2, 1])
        with cm: render_map(lat, lon, results)
        with cl:
            st.subheader("Results")
            for _, r in results.iterrows():
                others = [s.replace("_"," ").title() for s in sports if s != sport and str(r[s]).lower() in ['1','true','yes','y']]
                other_txt = ", ".join(others[:4]) + ("..." if len(others)>4 else "") if others else "None"
                
                with st.expander(f"**{r['name']}** ({r['dist_miles']:.2f} mi)", expanded=True):
                    st.write(f"📍 {r['address']}")
                    st.caption(f"Also here: {other_txt}")

if __name__ == "__main__":
    main()