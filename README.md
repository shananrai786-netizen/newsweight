# Newsweight: free setup (no money needed)

What you get: a site on Netlify that shows stories fetched, deduplicated and scored every ~5 minutes by a free GitHub job using Google's free Gemini API.

Expect stories to appear about 5 to 10 minutes after a paper publishes (5-minute schedule + GitHub delays + file caching). Free limits can change, so recheck them before launch.

## One-time setup (about 30 minutes)

1. **GitHub.** Create a free account and a new **public** repository called `newsweight` (public is what makes the scheduled job free). Upload everything in this folder to it (drag and drop in the web page works). Keep the `.github` folder.
2. **Data branch.** In the repo, click Add file > Create new file, name it `stories.json`, paste the contents of `stories.seed.json`, and choose "Create a new branch for this commit" named `data`.
3. **Gemini key.** Go to Google AI Studio, sign in, create an API key (free, no card). In your repo: Settings > Secrets and variables > Actions > New repository secret. Name `GEMINI_API_KEY`, value = the key.
   Optional: under Variables, add `GEMINI_MODEL` if the default (`gemini-2.5-flash-lite`) is retired. Pick a current Flash-Lite model name from AI Studio.
4. **Check the feeds.** Open `pipeline/feeds.json`. Make sure each address opens in your browser as XML and that the paper allows this use (see Legal below). Delete any that don't.
5. **Run it once.** Repo > Actions > update-news > Run workflow. After ~1 minute, open `https://raw.githubusercontent.com/YOUR_USER/newsweight/data/stories.json`. You should see stories.
6. **Point the site at it.** In `site/index.html` find `YOUR_GITHUB_USER` near the top and replace it with your GitHub username. Commit.
7. **Netlify.** Create a free account, Add new site > Import from GitHub > pick `newsweight`. Production branch `main`, publish directory `site` (already set in netlify.toml). Make sure branch deploys are off. Your site is live at `something.netlify.app`.

News updates go to the `data` branch, so they never trigger a Netlify deploy and never use up free credits.

## Legal (do before announcing)
- The job stores only your own wording (headline, summary, score) and never copies article text or photos. It still reads each paper's headline and snippet from its feed, so check each paper's terms for this use, and drop papers that object.
- Add a short about/terms page. The site stores reader choices only in their own browser, so you collect no personal data.
- Get a lawyer's view before you promote it widely.

## Checks
- The summaries are written from the headline and short snippet in each feed, so they can be thin. Stories where the model says the text was too short get a shortened summary. Read a sample every week.
- GitHub pauses scheduled jobs in a public repo after 60 days without activity. The bot's pushes should count, but check the Actions tab now and then.
- Gemini's free tier may use your inputs to improve Google products. That is fine for public news, but never send private data.
- Change which papers appear, topics or scoring guidance: edit `pipeline/feeds.json` and the prompt in `pipeline/run.py`.

## Files
- `site/index.html`: the website (the prototype plus the live loader)
- `pipeline/run.py`, `pipeline/feeds.json`: fetch, group duplicates, score
- `.github/workflows/news.yml`: runs the job every 5 minutes
- `netlify.toml`: hosting settings
