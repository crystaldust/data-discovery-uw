# Labelling guide

`labels.csv` holds 200 pairs of podcast feeds drawn by `uv run ddm eval-sample`. For each
pair, decide whether the **right** feed (the record being judged) and the **left** feed (the
first record of the entity it was matched or compared with) are the same podcast.

The easiest way is the local labelling page:

```bash
uv run ddm label            # opens http://127.0.0.1:8765/?token=... in your browser
```

It shows the two feeds side by side, field by field, highlights fields that match, links
to both Podcast Index pages (and Apple Podcasts where known), and saves every label to
`labels.csv` at once. Keys: `1` same, `2` different, `3` unsure, `0` clear, `←`/`→` (or
`K`/`J`) previous and next, `N` to write a note. It only listens on 127.0.0.1, and the
address carries a one-time token. Stop it with Ctrl-C; you can resume any time.

You can also edit `labels.csv` in a spreadsheet; the label goes in the `label` column:

| Label | Meaning |
|---|---|
| `same` | The same podcast |
| `different` | Different podcasts |
| `unsure` | Cannot tell from public information; say why in `note` |

Leave nothing else in `label`. `uv run ddm eval-report` rejects any other value and names
the line.

## What counts as the same podcast

Same:
- the show was renamed (same host, same episodes or a clear continuation);
- the show moved to another hosting platform or feed address;
- a mirror or a duplicate submission of the same feed.

Different:
- two shows from the same publisher or network;
- two shows with the same or a similar title but different hosts or topics;
- a show and a spin-off or a separate season feed that is published as its own podcast.

## How to check, in order

1. Titles and authors on both sides.
2. The website column.
3. The two Podcast Index pages (`left_pi_url`, `right_pi_url`): artwork, description and
   recent episodes.

You do not need to open the feeds themselves. Spend at most a minute or two on a pair; if
it is still unclear, label it `unsure`.

## Columns you can ignore

`stratum`, `method`, `score`, `title_sim`, `author_sim`, `site_match` and `right_class`
are what the system decided. They are there for the report; try not to let them sway the
label. The labelling page hides them unless you tick "System's view".
