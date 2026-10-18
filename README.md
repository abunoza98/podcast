# Adrian's Daily Brief

A private podcast that makes itself. Every morning before 6 am Sydney time, GitHub Actions:

1. pulls the last day's stories from news, football and wrestling feeds
2. ranks them (how many outlets cover it, how high the editors placed it, how fresh it is)
3. asks a Groq model to pick the stories that matter and drop the junk
4. reads the actual articles and writes a spoken script (three beats per story: what happened, context, what's next)
5. voices it with ElevenLabs (or Kokoro, free, if ElevenLabs fails or runs out of credits)
6. publishes the episode and `feed.xml` to the `gh-pages` branch, which GitHub Pages serves

Feed: **https://abunoza98.github.io/podcast/feed.xml**

## One-time setup

1. **Add your keys.** Repo → Settings → Secrets and variables → Actions → *New repository secret*. Add two:
   - `GROQ_API_KEY`
   - `ELEVENLABS_API_KEY`
2. **Make the first episode.** Actions tab → *Daily episode* → *Run workflow* → mode `run`. It takes about five minutes. (Mode `probe` just checks the feeds and publishes nothing.)
3. **Point Pages at the new branch.** Settings → Pages → Branch: `gh-pages`, folder `/ (root)` → Save. The first run creates that branch.
4. **Subscribe.** iPhone: Podcasts → Library → ••• → *Follow a Show by URL*. Mac: Podcasts → File → *Follow a Show by URL…*. Paste the feed URL. It subscribes silently, so check Library → Shows.

## Changing things

Everything lives in `config.toml`. Edit it on GitHub with the pencil icon; the next run uses it.

- **Topics:** each `[[sections]]` block has a `brief` (what the editor should pick), `picks` (how many stories) and `feeds`. Add or remove feeds freely. Run the workflow in `probe` mode afterwards to check they work.
- **Length:** `[length]` controls characters per story. About 900 characters is a minute.
- **Voice:** `[voice]`. Switch `engine` to `"kokoro"` to stop using ElevenLabs credits entirely.
- **Cover:** replace `cover.png` with any square image. It's resized for Apple automatically and gets a new file name, so the app picks up the change.
- **Time:** the `cron` line in `.github/workflows/daily-episode.yml` (UTC).

## Checking a run

Actions tab → click a run. The summary page lists the stories picked, why, the episode length, which voice was used, any feeds that failed, and the full script. GitHub emails you if a scheduled run fails.

## Costs

- Groq: free tier.
- GitHub: free (public repo).
- ElevenLabs: an episode is about 5,500 characters. Starter (30,000 credits) does not cover a daily episode. Check *Usage* in ElevenLabs after the first episode to see what one actually costs. When credits run out, Kokoro takes over automatically.

## Notes

- The repo and feed are public; anyone who finds the URL can listen. It's just news readouts.
- GitHub pauses scheduled workflows in public repos after 60 days without repository activity. The daily publish normally counts as activity, but if runs ever stop, re-enable the workflow from the Actions tab.
