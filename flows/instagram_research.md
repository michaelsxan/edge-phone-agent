# Instagram research actions (Instagram 452.1.0.53.64) — verified 2026-11-02 01:10–01:20

All read from the UI tree as text.

- **Search**: bottom tab `Search and explore` → `action_bar_search_edit_text` → type → Enter.
  Tabs: For you / Accounts / Audio / Tags. Result tiles: "Reel by <name> at row r, column c" + play count
  (`preview_clip_play_count`, e.g. 13.7M).
- **Open a reel**: tap the tile. Reel screen ids: `like_button`, `like_count` ("Like number is N. View likes"),
  `comment_button`, `comment_count` ("Comment number is N"), `direct_share_button` (Share/send — never tap),
  `ufi_text_component` (reshare count), `save_button` (never tap), `inline_follow_button` (never tap),
  author `clips_author_profile_pic`.
- **Next reel**: swipe up. Previous: swipe down.
- **Comments**: tap `comment_button` (~667, 868). Each comment is "<user> said <text>", date, "Author" tag for the
  creator, `Reply`, "Tap to like comment". Swipe to load more.
- **Replies**: "View N more replies" under a comment; tap to expand.
- **Like / unlike a reel**: tap `like_button`. Verify with `like_count` (+1 / back). `selected` stays false, so use the count.
- **Comment**: on a post, `row_feed_button_comment` opens the composer with the keyboard; type; tap the `Post`
  icon (`layout_comment_thread_post_button_icon`). Verify "<account> said <text>" appears.
- **Delete own comment**: long-press it → context menu `Delete` (no extra confirm).

## Traps
- **Double-tap on a reel likes it** ("Double tap to play or pause"): only ever single-tap.
- Emoji chips (`item_emoji`, e.g. ❤️) above the comment box post that emoji on tap.
- The keyboard's action key is labelled `Send` — don't press Enter in the comment box unless posting is intended.
- `Share`, `Save`, `Follow` sit next to like/comment on every reel.

## Automated (research.py) — 2-minute run passed 2026-11-02 17:53
- Captions on the reel viewer are truncated with "…"; the full caption needs a tap on "more".
- Comments: "<user> said <text>"; reply counts from "View N more replies".
