# My Audio Project

A small Streamlit app that reads playlist CSV files and searches Jamendo for openly licensed tracks that artists have explicitly allowed users to download.

## Important limitations

- A CSV contains track metadata, not audio.
- This app does not download or convert Spotify-protected music.
- Jamendo is an independent-music catalog. Most major-label commercial hits may not be available.
- Search results can be similar but not identical recordings. Check the artist, track, and license before downloading.
- The app only offers a download when Jamendo returns `audiodownload_allowed=true` and a download URL.
- Follow the license shown for each track. Some Creative Commons licenses restrict commercial use or derivatives.

## Get a Jamendo API client ID

1. Create an account at https://developer.jamendo.com/
2. Register an application and copy its client ID.
3. Keep the ID private. Enter it in the app when prompted; do not commit private credentials to GitHub.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy free with Streamlit Community Cloud

1. Push `app.py`, `requirements.txt`, and this README to your GitHub repository.
2. Visit https://share.streamlit.io/ and sign in.
3. Choose this repository, branch `main`, and file `app.py`.
4. Deploy. Open the app URL on your Android phone.
5. Upload one or more playlist CSV files, or the ZIP containing them.

The app understands common export columns such as `Track name` and `Artist name`, as well as `Song Title` and `Artist`.
