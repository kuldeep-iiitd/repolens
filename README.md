# RepoLens

Paste a public GitHub repo URL. RepoLens draws its architecture diagram and gives you a timeline slider showing how the architecture changed over the project's git history.

**How it works**
1. Clones the repo and picks 6 snapshots spread evenly across the git history.
2. Builds a file dependency graph from import statements (Python, JS/TS, Java and Go).
3. For each snapshot, sends the whole repo (file heads plus the graph) to Gemini in **one long-context call**. Gemini returns structured JSON: modules, file summaries, a reading order and a note on what changed since the previous snapshot.
4. Draws the module graph. The slider moves between snapshots.

## Run locally
```bash
pip install -r requirements.txt
# put your key in .env:  GEMINI_API_KEY=...   (free key: https://aistudio.google.com/apikey)
python core.py https://github.com/psf/requests   # optional: precompute a demo into cache/
streamlit run app.py
```

## Deploy free (Streamlit Community Cloud)
1. Push this folder to a GitHub repo, **including `cache/`**, so the demo repos load instantly.
2. On share.streamlit.io, create a new app pointing to `app.py`.
3. Under Settings → Secrets, add `GEMINI_API_KEY = "..."`.

Model choice is automatic: at startup RepoLens pings several Gemini Flash models and uses the fastest one, falling back to the others if it gets busy. To change the list, set `GEMINI_MODELS=model-a,model-b` in `.env`.
