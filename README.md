# ANIFLIX INFINITY Poster Bot

Telegram anime-poster bot that searches TMDB and creates 16:9 (1920×1080) posters with anime artwork, synopsis, year, rating, genres, quality badges, a Telegram join banner, and the supplied **ANIFLIX INFINITY** logo.

## Setup
1. Create a bot with Telegram @BotFather and copy its token.
2. Create a TMDB account and get an API Read Access Token or API key: https://developer.themoviedb.org/docs/getting-started
3. Extract this ZIP and copy `.env.example` to `.env`.
4. Add `TELEGRAM_BOT_TOKEN`, `TMDB_READ_ACCESS_TOKEN` (or `TMDB_API_KEY`) and your numeric Telegram user ID as `ADMIN_USER_ID`.
5. Install Python 3.11+, then run `pip install -r requirements.txt`.
6. Start the bot with `python bot.py`.

Never publish your `.env` file or share your bot token.

## Edit channel usernames and links
Only the Telegram account whose numeric ID matches `ADMIN_USER_ID` can change these settings. You can edit them without changing the source code:

- `/setusername @YourChannelUsername` — changes the username printed on the poster and join banner.
- `/setlinks https://t.me/WatchChannel | https://t.me/JoinChannel` — changes the Watch Now and Join Telegram buttons. Keep the `|` separator between the two links.
- `/setbrand ANIFLIX INFINITY` — changes the small poster watermark text. The supplied logo image remains unchanged.
- `/settings` — displays the current saved settings.

Settings are saved in `bot_settings.json` inside `TEMPLATES_DIR` and remain after restarts when a persistent disk is attached.

## Saved poster templates
Template management is restricted to `ADMIN_USER_ID`.

- `/addtemplate [name]` — start adding a named blank template, then send its image.
- `/templates` — list saved templates.
- `/settemplate [name]` — activate a saved template; without a name, use the buttons.
- `/deletetemplate [name]` — delete a template; without a name, use the buttons.
- `/cancel` — cancel template setup.

Use a 16:9 blank template (recommended 1920×1080, minimum 640×360) with space reserved for artwork on the left and generated text/details on the right. The bot places the selected anime artwork and details over the background template; it does not automatically detect layout zones in the image. There is no application-level template count limit.

For Render persistence, attach a persistent disk mounted at `/var/data` and set `TEMPLATES_DIR=/var/data/templates`. Without a persistent disk, saved templates and settings may be lost when the service is redeployed.

## Bot commands
- `/start` or `/help` — show help
- `/poster Naruto` — search and create a poster
- `/search Naruto` — same poster search workflow
- `/synopsis Naruto` — show synopsis and metadata

## Environment variables
See `.env.example`. `CHANNEL_USERNAME`, `WATCH_NOW_URL`, and `JOIN_TELEGRAM_URL` are optional defaults; they can be changed later with the admin commands above.

TMDB supplies metadata and poster artwork. Check TMDB's terms and artwork rights before public or commercial distribution.
