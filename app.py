import io, re, zipfile, unicodedata
from pathlib import Path
from urllib.parse import quote
import pandas as pd
import requests
import streamlit as st

JAMENDO = "https://api.jamendo.com/v3.0/tracks/"
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata/"

st.set_page_config(page_title="My Audio Project", page_icon="🎵", layout="wide")
st.title("🎵 My Audio Project")
st.write("Search for exact matches that are available as free downloads.")
st.info("This app does not convert Spotify tracks or bypass Spotify protections. It searches Jamendo and Internet Archive. Mainstream commercial recordings may not be available.")

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.casefold()).split())

def text(v):
    return " ".join(map(str, v)) if isinstance(v, list) else str(v or "")

def col(df, names):
    d = {str(c).strip().lower(): c for c in df.columns}
    return next((d[n] for n in names if n in d), None)

def parse_csv(raw, filename):
    df = pd.read_csv(io.BytesIO(raw))
    tc = col(df, ["track name", "song title", "track", "title", "name"])
    ac = col(df, ["artist name", "artist", "artists"])
    if tc is None or ac is None:
        return None, f"{filename}: title/artist columns not found."
    out = pd.DataFrame({"Playlist": Path(filename).stem, "Track": df[tc], "Artist": df[ac]})
    out = out.dropna()
    out["Track"] = out["Track"].astype(str).str.strip()
    out["Artist"] = out["Artist"].astype(str).str.strip()
    return out[(out.Track != "") & (out.Artist != "")], None

def read_uploads(items):
    files = []
    for item in items:
        raw = item.getvalue()
        if item.name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                files.extend((Path(n).name, z.read(n)) for n in z.namelist()
                              if n.lower().endswith(".csv") and not n.startswith("__MACOSX/"))
        else:
            files.append((item.name, raw))
    return files

def jamendo(client, track, artist):
    p = {"client_id": client, "format": "json", "limit": 50, "search": track,
         "artist_name": artist, "include": "licenses", "audiodlformat": "mp32"}
    r = requests.get(JAMENDO, params=p, timeout=30); r.raise_for_status()
    data = r.json()
    if data.get("headers", {}).get("status") != "success":
        raise RuntimeError(data.get("headers", {}).get("error_message", "Jamendo API error"))
    out = []
    for x in data.get("results", []):
        if norm(x.get("name")) == norm(track) and norm(x.get("artist_name")) == norm(artist):
            if x.get("audiodownload_allowed") and x.get("audiodownload"):
                out.append({"source":"Jamendo", "title":x.get("name"), "artist":x.get("artist_name"),
                            "license":x.get("license_ccurl",""), "url":x["audiodownload"],
                            "page":f"https://www.jamendo.com/track/{x.get('id','')}"})
    return out

def ia_exact(track, artist):
    q = f'mediatype:audio AND title:"{track}" AND creator:"{artist}"'
    p = [("q",q),("output","json"),("rows",10),("fl[]","identifier")]
    r = requests.get(IA_SEARCH, params=p, timeout=30); r.raise_for_status()
    docs = r.json().get("response",{}).get("docs",[])
    out = []
    for d in docs:
        ident = d.get("identifier")
        if not ident: continue
        mr = requests.get(IA_META + quote(ident), timeout=30); mr.raise_for_status()
        data = mr.json(); m = data.get("metadata",{})
        title, creator = text(m.get("title")), text(m.get("creator"))
        lic = text(m.get("licenseurl") or m.get("license_url"))
        low = lic.lower()
        recognized = any(x in low for x in [
            "creativecommons.org/publicdomain/zero/",
            "creativecommons.org/publicdomain/mark/",
            "creativecommons.org/licenses/by/",
            "creativecommons.org/licenses/by-sa/",
            "creativecommons.org/licenses/by-nc/",
            "creativecommons.org/licenses/by-nc-sa/",
            "creativecommons.org/licenses/by-nd/",
            "creativecommons.org/licenses/by-nc-nd/",
        ])
        if norm(title) != norm(track) or norm(creator) != norm(artist) or not recognized:
            continue
        for f in data.get("files",[]):
            name = f.get("name","")
            if name.lower().endswith(".mp3") and not f.get("private"):
                out.append({"source":"Internet Archive", "title":title, "artist":creator,
                            "license":lic, "url":f"https://archive.org/download/{quote(ident)}/{quote(name)}",
                            "page":f"https://archive.org/details/{quote(ident)}"})
    return out

client = st.text_input("Jamendo API client ID", type="password")
sources = st.multiselect("Sources", ["Jamendo","Internet Archive"], ["Jamendo","Internet Archive"])
uploads = st.file_uploader("Upload separated playlist CSVs or their ZIP", type=["csv","zip"], accept_multiple_files=True)

if uploads:
    frames=[]
    for filename, raw in read_uploads(uploads):
        try:
            frame, err = parse_csv(raw, filename)
            if err: st.warning(err)
            elif frame is not None: frames.append(frame)
        except Exception as e: st.warning(f"{filename}: {e}")
    if frames:
        library = pd.concat(frames, ignore_index=True).drop_duplicates()
        st.success(f"Loaded {len(library)} tracks from {library.Playlist.nunique()} playlist files.")
        st.dataframe(library.head(20), use_container_width=True, hide_index=True)
        names = ["All playlists"] + sorted(library.Playlist.unique())
        chosen = st.selectbox("Choose a playlist", names)
        view = library if chosen == "All playlists" else library[library.Playlist == chosen]
        count = st.number_input("Maximum tracks to search", 1, min(100,len(view)), min(10,len(view)))
        st.caption("Only exact normalized title + artist/creator matches are eligible. Internet Archive results also need an explicit recognized open/public-domain license and an MP3 file.")
        if st.button("Search selected free sources", type="primary", disabled=not sources):
            results=[]
            progress=st.progress(0); status=st.empty()
            for i,row in view.head(int(count)).reset_index(drop=True).iterrows():
                status.write(f"Searching {i+1}/{count}: {row.Track} - {row.Artist}")
                found=False
                if "Jamendo" in sources:
                    if client:
                        try:
                            for x in jamendo(client,row.Track,row.Artist):
                                found=True; results.append({"Playlist":row.Playlist,"Requested track":row.Track,"Requested artist":row.Artist,**x,"Match status":"Exact match; download permitted","Download allowed":"Yes"})
                        except Exception as e:
                            results.append({"Playlist":row.Playlist,"Requested track":row.Track,"Requested artist":row.Artist,"source":"Jamendo","Match status":f"Search error: {e}","Download allowed":"No"})
                    else:
                        results.append({"Playlist":row.Playlist,"Requested track":row.Track,"Requested artist":row.Artist,"source":"Jamendo","Match status":"Skipped: Client ID required","Download allowed":"No"})
                if "Internet Archive" in sources:
                    try:
                        for x in ia_exact(row.Track,row.Artist):
                            found=True; results.append({"Playlist":row.Playlist,"Requested track":row.Track,"Requested artist":row.Artist,**x,"Match status":"Exact match; open license listed","Download allowed":"Yes"})
                    except Exception as e:
                        results.append({"Playlist":row.Playlist,"Requested track":row.Track,"Requested artist":row.Artist,"source":"Internet Archive","Match status":f"Search error: {e}","Download allowed":"No"})
                if not found:
                    results.append({"Playlist":row.Playlist,"Requested track":row.Track,"Requested artist":row.Artist,"source":"Combined search","Match status":"No eligible exact free match found","Download allowed":"No"})
                progress.progress((i+1)/int(count))
            status.success("Search finished. Review each source page and license before downloading.")
            st.session_state["results"] = pd.DataFrame(results)

if "results" in st.session_state:
    df=st.session_state["results"]
    st.subheader("Search results")
    st.metric("Eligible free-download results", int((df.get("Download allowed")=="Yes").sum()))
    show=df.drop(columns=["url"], errors="ignore")
    st.dataframe(show,use_container_width=True,hide_index=True)
    st.download_button("Download results CSV",show.to_csv(index=False).encode("utf-8-sig"),
                       "free_music_search_results.csv","text/csv")
    st.subheader("Eligible downloads")
    eligible=df[df.get("Download allowed")=="Yes"]
    if eligible.empty:
        st.info("No eligible exact free downloads found. No unrelated tracks will be substituted.")
    else:
        for i,x in eligible.iterrows():
            with st.expander(f"{x.get('title','')} - {x.get('artist','')} ({x.get('source','')})"):
                st.write("Playlist:",x.get("Playlist",""))
                st.write("License:",x.get("license",""))
                st.markdown(f"[Open source item]({x.get('page','')})")
                st.warning("Review the source item and license terms before downloading.")
                try:
                    r=requests.get(x["url"],timeout=90); r.raise_for_status()
                    fn=x["url"].split("/")[-1]
                    st.download_button("Download authorized MP3",r.content,fn or "audio.mp3","audio/mpeg",key=f"dl{i}")
                except Exception as e: st.warning(f"Download unavailable: {e}")
