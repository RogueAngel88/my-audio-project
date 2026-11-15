import io
import csv
import re
import zipfile
import unicodedata
from urllib.parse import quote
from pathlib import Path
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Free Music Finder", page_icon="🎵", layout="wide")
st.title("🎵 Free Music Finder")
st.caption("Find exact matches from free-download catalogs. No Spotify audio is extracted.")

JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"
IA_SEARCH = "https://archive.org/advancedsearch.php"
CCMIXTER_API = "https://ccmixter.org/api/query"

def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

def find_col(df, choices):
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for choice in choices:
        if choice in lookup:
            return lookup[choice]
    return None

def read_playlists(uploads):
    playlists = []
    for upload in uploads:
        archive_name = Path(upload.name).stem
        with zipfile.ZipFile(upload) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv") or name.startswith("__MACOSX/"):
                    continue
                try:
                    raw = zf.read(name)
                    df = pd.read_csv(io.BytesIO(raw))
                except Exception:
                    continue
                title_col = find_col(df, ["track name", "song title", "title", "track"])
                artist_col = find_col(df, ["artist name", "artist", "artists"])
                if title_col is None or artist_col is None:
                    continue
                for _, row in df.iterrows():
                    title = str(row.get(title_col, "")).strip()
                    artist = str(row.get(artist_col, "")).strip()
                    if title and artist and title.lower() != "nan" and artist.lower() != "nan":
                        playlists.append({
                            "Playlist": f"{archive_name} / {Path(name).stem}",
                            "Requested track": title,
                            "Requested artist": artist
                        })
    # Remove repeated CSV rows within the same playlist, keeping first occurrence.
    seen, unique = set(), []
    for item in playlists:
        key = (norm(item["Playlist"]), norm(item["Requested track"]), norm(item["Requested artist"]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return pd.DataFrame(unique)

def jamendo_search(title, artist, client_id):
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": 50,
        "search": title,
        "audioformat": "mp32",
        "include": "licenses",
    }
    try:
        response = requests.get(JAMENDO_API, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return []
    matches, seen = [], set()
    for track in data.get("results", []):
        track_title = track.get("name", "")
        track_artist = track.get("artist_name", "")
        # Exact normalized title and artist required. No fuzzy or title-only matches.
        if norm(track_title) != norm(title) or norm(track_artist) != norm(artist):
            continue
        if not track.get("audiodownload_allowed") or not track.get("audiodownload"):
            continue
        track_id = str(track.get("id", ""))
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        matches.append({
            "Source": "Jamendo",
            "Matched title": track_title,
            "Matched artist": track_artist,
            "License": track.get("license_ccurl") or "See Jamendo track page",
            "Source page": track.get("shareurl") or "",
            "Download URL": track.get("audiodownload"),
            "Status": "Jamendo marks audio download as allowed"
        })
    return matches

def archive_discovery(title, artist):
    # Internet Archive is discovery-only. Its uploader-supplied metadata/license
    # cannot independently prove rights, so no IA item is ever download-eligible.
    query = f'mediatype:audio AND title:"{title}" AND creator:"{artist}"'
    params = {"q": query, "fl[]": ["identifier", "title", "creator"], "rows": 20, "output": "json"}
    try:
        response = requests.get(IA_SEARCH, params=params, timeout=25)
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
    except Exception:
        return []
    found, seen = [], set()
    for doc in docs:
        identifier = str(doc.get("identifier", ""))
        if not identifier or identifier in seen:
            continue
        title_value = doc.get("title", "")
        creator_value = doc.get("creator", "")
        if isinstance(title_value, list):
            title_value = title_value[0] if title_value else ""
        if isinstance(creator_value, list):
            creator_value = creator_value[0] if creator_value else ""
        if norm(title_value) != norm(title) or norm(creator_value) != norm(artist):
            continue
        seen.add(identifier)
        found.append({
            "Source": "Internet Archive (unverified)",
            "Matched title": title_value,
            "Matched artist": creator_value,
            "License": "Uploader metadata only; not independently verified",
            "Source page": f"https://archive.org/details/{quote(identifier)}",
            "Download URL": "",
            "Status": "Discovery only. Not eligible for download."
        })
    return found



def ccmixter_discovery(title, artist):
    """
    Search ccMixter's documented Query API. Search by exact title phrase first,
    then compare returned title/artist fields locally. ccMixter API metadata
    fields vary by dataview, so retain diagnostics rather than silently
    treating a parse failure as 'no matches'.
    """
    params = {
        "f": "csv",
        "dataview": "default",
        "limit": 50,
        "search": title,
        "search_type": "match",
    }
    diagnostics = {"api_status": "not checked", "columns": [], "rows_received": 0, "error": ""}
    try:
        response = requests.get(
            CCMIXTER_API,
            params=params,
            timeout=25,
            headers={"User-Agent": "FreeMusicFinder/1.0"}
        )
        diagnostics["api_status"] = str(response.status_code)
        response.raise_for_status()
        text_data = response.content.decode("utf-8-sig", errors="replace").strip()
        if not text_data:
            diagnostics["error"] = "The API returned an empty response."
            return [], diagnostics
        rows = list(csv.DictReader(io.StringIO(text_data)))
        if not rows:
            diagnostics["error"] = "The response contained no CSV data rows."
            return [], diagnostics
        diagnostics["columns"] = list(rows[0].keys())
        diagnostics["rows_received"] = len(rows)
        # Show a small, safe metadata sample in the app diagnostics so we can
        # confirm which API field represents the title and uploader/artist.
        sample_fields = (
            "upload_id", "upload_name", "user_name", "user_real_name",
            "artist_page_url", "license_name", "license_url", "file_page_url"
        )
        diagnostics["sample_rows"] = [
            {field: str(row.get(field, "") or "")[:300] for field in sample_fields}
            for row in rows[:3]
        ]
    except Exception as exc:
        diagnostics["error"] = f"{type(exc).__name__}: {exc}"
        return [], diagnostics

    found, seen = [], set()
    for row in rows:
        lower = {str(k).strip().lower(): v for k, v in row.items() if k}
        name = next((lower[k] for k in (
            "upload_name", "name", "title", "upload_title"
        ) if lower.get(k)), "")
        creator = next((lower[k] for k in (
            "user_real_name", "artist", "artist_name", "fullname",
            "user_full_name", "upload_artist", "user_name", "user"
        ) if lower.get(k)), "")
        upload_id = next((lower[k] for k in (
            "upload_id", "id"
        ) if lower.get(k)), "")
        license_code = next((lower[k] for k in (
            "upload_license", "license", "lic"
        ) if lower.get(k)), "")

        if norm(name) != norm(title) or norm(creator) != norm(artist):
            continue

        identity = str(upload_id or f"{norm(name)}|{norm(creator)}")
        if identity in seen:
            continue
        seen.add(identity)

        license_names = {
            "by": "CC BY (Attribution)",
            "nc": "CC BY-NC (NonCommercial)",
            "sa": "CC BY-SA (ShareAlike)",
            "nod": "CC BY-ND (NoDerivatives)",
            "byncsa": "CC BY-NC-SA",
            "byncnd": "CC BY-NC-ND",
            "s": "Sampling",
            "splus": "Sampling+",
            "ncsplus": "NonCommercial Sampling+",
            "pd": "Public Domain",
        }
        license_label = license_names.get(
            str(license_code).lower(),
            f"License code: {license_code or 'not returned'}"
        )
        item_url = (
            f"https://ccmixter.org/files/{quote(str(creator))}/{quote(str(upload_id))}"
            if upload_id else
            "https://ccmixter.org/api/query?" + requests.compat.urlencode({
                "f": "page", "t": "search_uploads", "limit": 30,
                "search_type": "match", "search": title
            })
        )
        found.append({
            "Source": "ccMixter (discovery)",
            "Matched title": name,
            "Matched artist": creator,
            "License": license_label,
            "Source page": item_url,
            "Download URL": "",
            "Status": "Discovery only. Review the individual upload page and license."
        })
    diagnostics["exact_matches"] = len(found)
    return found, diagnostics

with st.sidebar:
    st.header("Settings")
    client_id = st.text_input("Jamendo Client ID", type="password", help="Enter your own Jamendo Client ID. It is not stored in this app.")
    include_ia = st.checkbox("Show Internet Archive discovery results (not downloadable)", value=False)
    include_ccmixter = st.checkbox("Search ccMixter (license review required)", value=True)
    max_tracks = st.selectbox("Tracks to test", [10, 25, 50, 100, "All"], index=0)

uploads = st.file_uploader("Upload your Spotify playlist ZIP files", type=["zip"], accept_multiple_files=True)
if uploads:
    try:
        tracks_df = read_playlists(uploads)
    except Exception as exc:
        st.error(f"Could not read ZIP: {exc}")
        tracks_df = pd.DataFrame()
    if tracks_df.empty:
        st.error("No usable CSV tracks found. CSVs need columns such as Track name and Artist name.")
    else:
        st.success(f"Loaded {len(tracks_df):,} unique tracks from {tracks_df['Playlist'].nunique()} playlists across {len(uploads)} ZIP file(s).")
        playlist_names = sorted(tracks_df["Playlist"].unique().tolist())
        selected = st.multiselect("Choose playlist(s)", playlist_names, default=playlist_names[:1])
        subset = tracks_df[tracks_df["Playlist"].isin(selected)].copy()
        if max_tracks != "All":
            subset = subset.head(int(max_tracks))
        st.write(f"Tracks queued for search: **{len(subset)}**")
        if st.button("Search selected free sources", type="primary", disabled=not client_id.strip()):
            if not client_id.strip():
                st.warning("Enter your Jamendo Client ID in the sidebar.")
            else:
                rows, eligible, discovery, ccmixter_results = [], [], [], []
                progress = st.progress(0)
                status = st.empty()
                total = max(len(subset), 1)
                for index, (_, item) in enumerate(subset.iterrows(), start=1):
                    title, artist = item["Requested track"], item["Requested artist"]
                    jamendo = jamendo_search(title, artist, client_id.strip())
                    ia = archive_discovery(title, artist) if include_ia else []
                    cc, cc_diag = ccmixter_discovery(title, artist) if include_ccmixter else ([], {"api_status": "disabled", "columns": [], "rows_received": 0, "error": ""})
                    for match in cc:
                        ccmixter_results.append({**item.to_dict(), **match, "Download allowed": "No"})
                    if jamendo:
                        for match in jamendo:
                            record = {**item.to_dict(), **match, "Download allowed": "Yes"}
                            eligible.append(record)
                        rows.append({**item.to_dict(), "Source": "Jamendo", "Match status": f"{len(jamendo)} exact downloadable match(es)", "Download allowed": "Yes"})
                    elif ia:
                        # Collapse multiple Archive uploads for the same requested
                        # title/artist into one discovery row. Keep one source page
                        # and report how many distinct archive records matched.
                        unique_archive = {}
                        for match in ia:
                            key = (norm(match.get("Matched title", "")), norm(match.get("Matched artist", "")))
                            if key not in unique_archive:
                                unique_archive[key] = match
                        for match in unique_archive.values():
                            record = {**item.to_dict(), **match, "Download allowed": "No"}
                            record["Additional archive records"] = max(len(ia) - 1, 0)
                            discovery.append(record)
                        rows.append({**item.to_dict(), "Source": "Internet Archive", "Match status": "Unverified discovery result only", "Download allowed": "No"})
                    elif cc:
                        rows.append({**item.to_dict(), "Source": "ccMixter", "Match status": "Unverified ccMixter discovery result only", "Download allowed": "No"})
                    else:
                        source_labels = ["Jamendo"]
                        if include_ia:
                            source_labels.append("Internet Archive")
                        if include_ccmixter:
                            source_labels.append("ccMixter")
                        rows.append({**item.to_dict(), "Source": " + ".join(source_labels), "Match status": "No eligible exact free match found", "Download allowed": "No"})
                    progress.progress(index / total)
                    status.caption(f"Checked {index} of {total}: {title} - {artist}")
                st.session_state["search_rows"] = rows
                st.session_state["eligible_rows"] = eligible
                st.session_state["discovery_rows"] = discovery
                st.session_state["ccmixter_rows"] = ccmixter_results
                st.session_state["ccmixter_diagnostics"] = cc_diag
                st.session_state["search_key"] = (tuple(selected), len(subset))
                status.success("Search finished. Jamendo download permissions are shown as reported by Jamendo. Review source pages and license terms.")
                progress.empty()

if "search_rows" in st.session_state:
    st.subheader("Search results")
    results = pd.DataFrame(st.session_state["search_rows"])
    st.metric("Tracks checked", len(results))
    st.dataframe(results, use_container_width=True, hide_index=True)
    st.download_button("Download results CSV", results.to_csv(index=False).encode("utf-8-sig"), "music_search_results.csv", "text/csv")
    eligible = st.session_state.get("eligible_rows", [])
    st.subheader("Jamendo exact-match downloads")
    if eligible:
        for i, item in enumerate(eligible):
            with st.expander(f"{item['Matched title']} - {item['Matched artist']} (Jamendo)"):
                st.write(f"Playlist: {item['Playlist']}")
                st.write(f"License: {item['License']}")
                if item["Source page"]:
                    st.markdown(f"[Open Jamendo source page]({item['Source page']})")
                st.warning("Jamendo reports that downloads are allowed for this track. Review the track page and license before use.")
                st.link_button("Open Jamendo download", item["Download URL"])
    else:
        st.info("No exact Jamendo matches with download permission were found.")
    discovery = st.session_state.get("discovery_rows", [])
    if discovery:
        st.subheader("Internet Archive discovery only (not eligible for download)")
        st.warning("These are unverified uploader listings. No MP3 download links are provided.")
        df_discovery = pd.DataFrame(discovery)
        if not df_discovery.empty:
            df_discovery = df_discovery.drop_duplicates(
                subset=["Playlist", "Requested track", "Requested artist", "Matched title", "Matched artist"],
                keep="first"
            )
        st.dataframe(df_discovery.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        shown_links = set()
        for item in discovery:
            link_key = (norm(item["Requested track"]), norm(item["Requested artist"]))
            if link_key in shown_links:
                continue
            shown_links.add(link_key)
            st.markdown(f"- [{item['Requested track']} - {item['Requested artist']}]({item['Source page']})")
    cc_rows = st.session_state.get("ccmixter_rows", [])
    st.subheader("ccMixter discovery (license review required)")
    if cc_rows:
        st.warning("ccMixter results are discovery-only in this version. Open the source page to review the individual upload, attribution requirements, license restrictions, and download options. No automatic MP3 download links are provided.")
        cc_df = pd.DataFrame(cc_rows).drop_duplicates(
            subset=["Playlist", "Requested track", "Requested artist", "Matched title", "Matched artist"],
            keep="first"
        )
        st.dataframe(cc_df.drop(columns=["Download URL"], errors="ignore"), use_container_width=True, hide_index=True)
        for item in cc_df.to_dict("records"):
            st.markdown(f"- [{item['Matched title']} - {item['Matched artist']}]({item['Source page']})")
    else:
        st.info("No exact ccMixter matches were returned for this test.")
    cc_diag = st.session_state.get("ccmixter_diagnostics", {})
    with st.expander("ccMixter API diagnostics"):
        st.write(f"HTTP status: {cc_diag.get('api_status', 'not available')}")
        st.write(f"CSV rows received for the last checked track: {cc_diag.get('rows_received', 0)}")
        st.write(f"Exact matches: {cc_diag.get('exact_matches', 0)}")
        if cc_diag.get("columns"):
            st.write("Returned columns:", ", ".join(str(c) for c in cc_diag["columns"]))
        if cc_diag.get("sample_rows"):
            st.write("Sample metadata from the last checked track:")
            st.dataframe(pd.DataFrame(cc_diag["sample_rows"]), use_container_width=True, hide_index=True)
        if cc_diag.get("error"):
            st.error(cc_diag["error"])

st.caption("Important: exact metadata matches do not establish copyright ownership. Internet Archive listings are never treated as authorized downloads by this app. Jamendo download eligibility is based on Jamendo's API permission flag and should still be checked on the source page.")
