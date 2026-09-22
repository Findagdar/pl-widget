# Premier League matchday widget

A compact, embeddable head-to-head widget for any Premier League fixture —
standings, form, a projected-lineup pitch, key men and a handful of
data-derived "punter angles." Built as plain static files so it can be
hosted anywhere and dropped into any sportsbook page as an `<iframe>`.

```
pl-widget/
├── index.html                    the widget itself (self-contained HTML/CSS/JS)
├── teams.json                    static metadata for all 20 clubs (colors, short codes)
├── data.json                     standings / fixtures / lineups — this is what gets refreshed
├── scripts/fetch_data.py         the refresh script
└── .github/workflows/refresh.yml optional: runs the script every 2 days via GitHub Actions
```

## Why this isn't a Claude-hosted link

A Claude artifact (the kind of page you can preview inside Claude) lives on
`claude.ai`, is private by default, and can't be embedded in a third-party
`<iframe>` — there's no way to make that work from either side. These are
instead plain files meant to be hosted on **your own** infrastructure
(your own domain, S3/CloudFront, Netlify, Vercel, GitHub Pages — anything
that serves static files), which is what actually lets a sportsbook iframe
it.

## 1. Host it

Copy the whole `pl-widget/` folder to wherever you serve static files.
`index.html` fetches `./teams.json` and `./data.json` with relative paths,
so keep all three files (plus `scripts/` if you want the refresh job) in
the same folder. It needs to be served over `http(s)://`, not opened as a
local `file://` — browsers block `fetch()` of local JSON files.

## 2. Embed it

```html
<iframe
  src="https://YOUR-DOMAIN/pl-widget/index.html?home=liverpool&away=manchester-city"
  width="380" height="900" style="border:0;" loading="lazy"
  title="Liverpool vs Manchester City matchday widget">
</iframe>
```

**Size** — the widget is fluid: it reflows to whatever `width` the iframe
is given (no fixed pixel width baked in), down to phone width. `height`
follows content, so either:

- pick a generous fixed `height` for the slot you have (a full widget with
  a lineup pitch runs roughly 850–950px tall at default density), or
- auto-size it: the page posts its content height to the parent window on
  load and on resize —

  ```html
  <script>
    window.addEventListener('message', (e) => {
      if (e.data?.type === 'pl-widget-resize') {
        document.getElementById('pl-iframe').style.height = e.data.height + 'px';
      }
    });
  </script>
  ```

**Density** — add `&density=compact` for a tighter version (smaller type,
less padding) if you're placing it in a narrow sidebar slot, or
`&density=cozy` for a looser one. Default sits in between.

**Theme** — the widget follows the embedding page's light/dark setting
automatically. Force one with `&theme=dark` or `&theme=light` if your
site doesn't expose that and you want a fixed look.

## 3. Pick the fixture

- `?home=<slug>&away=<slug>` — show that exact pairing (see `teams.json`
  for the 20 valid slugs, e.g. `manchester-city`, `liverpool`, `arsenal`…).
- `?team=<slug>` — show that team's next fixture, whoever the opponent is
  (looked up from `data.json`'s `fixtures` block).
- No params — falls back to the first fixture found in `data.json`.

So one build covers all 20 clubs and every possible pairing; you just
change the query string per placement (e.g. generate the `<iframe src>`
dynamically from whatever match a given page is about).

## 4. Keep the data current

`data.json` ships with an early-season **example snapshot** so the widget
renders immediately — it is not live data. Two ways to refresh it every 2
days:

**Automatic (recommended):** if you host with git-based deploys (GitHub
Pages, Netlify, Vercel, Cloudflare Pages), the included
`.github/workflows/refresh.yml` runs `scripts/fetch_data.py` on a
schedule, commits the new `data.json`, and your host redeploys on push.
Nothing to babysit once it's set up — just enable Actions on the repo.

**Manual/cron:** run it yourself anywhere Python is available:

```bash
pip install requests beautifulsoup4 pandas lxml
python scripts/fetch_data.py          # overwrites data.json
python scripts/fetch_data.py --dry-run # prints instead, for checking output
```
then sync `data.json` to wherever you're hosting.

### What the script actually refreshes — and what it doesn't

- **Standings** (position, W-D-L, goals, points): scraped from Wikipedia's
  season table. This is the reliable part — plain HTML table, no anti-bot
  wall.
- **Fixtures** (next opponent/date/venue): left as a stub
  (`fetch_fixtures()` in the script) for you to fill in against whatever
  source you're comfortable scraping — check its `robots.txt` and terms
  first. A free structured alternative worth considering instead of
  scraping: [football-data.org](https://www.football-data.org/) (has a
  no-cost API tier).
- **Lineups**: **not** automated. Confirmed lineups typically only appear
  about an hour before kickoff, so a once-per-2-days job can't chase them
  reliably — the widget is built to fall back to a "check back closer to
  kick-off" placeholder whenever a team's `lineups` entry is missing from
  `data.json`. Curate these by hand for marquee fixtures, or wire up a
  lineups-capable API if you need this automated too.
- **Key player spotlight**: also manual/curated (`keyPlayer` in
  `data.json`) — the widget just omits that card's detail if a team isn't
  in there.

Sofascore itself doesn't offer a public API and its team pages return
403s to scripted requests, so it isn't used as a source here.

## Notes on the seed data

The standings currently in `data.json` reflect an early-season Wikipedia
snapshot (~5 Sep 2026) and are for demonstrating the widget's layout, not
today's real table. Run the refresh script (or wait for the scheduled
job) to replace them. Lineups and key-player cards are only filled in for
the Liverpool–Manchester City fixture used while building this — add more
as you curate them.
