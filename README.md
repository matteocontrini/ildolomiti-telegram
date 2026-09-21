# Il Dolomiti Telegram

This repository contains the robot that powers the [@TODO](https://t.me/) Telegram channel.

Compared to a basic RSS feed to Telegram publisher, and the [official Telegram channel](https://t.me/ildolomitinews), it features:

- Duplicate article detection based on Drupal's node ID. When the title of an article changes, the already sent Telegram message is modified.
- Retry at next round when an article fetch fails.
- Download images and upload them "manually" to Telegram API to avoid fetch failures. Fallback to a placeholder image if the image couldn't be downloaded.
- Cache busting on article URLs, to avoid incurring into 404. In previous implementations, if you requested an article too soon it would 404 and stay 404 in the edge cache for that particular request.
- Tag parsing.

Not all articles are published immediately. Some are saved for later and published in a daily digest message:
- Articles marked as Trento or Bolzano are published immediately.
- Articles marked by the website with area Belluno, Sondrio, or FVG are routed to the Veneto, Lombardia and Friuli-Venezia Giulia digest sections.
- When an article has no area marker, an LLM classifies its title, description and a short body excerpt as Trento, Bolzano, Veneto, Lombardia, Friuli-Venezia Giulia, Lago di Garda,
Tirolo, Italia or Altro. Classification failures fall back to Altro.
- Anything outside Trento and Bolzano is stored for a daily digest, grouped by area and sent in the evening.

## Running

```shell
uv sync
BOT_TOKEN=... OPENROUTER_API_KEY=... uv run main.py
```

`OPENROUTER_MODELS` optionally overrides the comma-separated fallback order. See the source code for the default.

## Testing

```shell
uv run python -m unittest discover -s tests -q
```
