# TikTok research actions (TikTok 47.4.3) — verified 2026-11-02 01:00–01:08

All read from the UI tree as text; no vision needed.

- **Search**: home top-right `Search` (`k_g`, ~671,128) → type → Enter. Results tabs: Top / Videos / Users / Shop / Photos / Sounds.
  Result cells expose caption (`desc`), creator (`bgg`), publish date, like count (`nck`, e.g. "x3.3K").
- **Open a result**: tap the cell. Video screen exposes creator (`… profile`), `Like video. N likes`,
  `Read or add comments. N comments`, `Share video. N shares`, sound, caption (`desc`).
- **Next video**: swipe up (360,1100 → 360,300).
- **Comments**: tap `Read or add comments…` (~664, 1048). Each comment: author (`title`), text (`f62`), date (`ep7`),
  likes (`tv_like_count`), `View N replies` (`tv_reply_comment_title`). Swipe inside the sheet to load more.
- **Replies**: tap `View N replies`; replies appear indented with a `Hide` link.
- **Like**: tap `Like video. N likes` (`g8m`, ~664, 943) → label becomes `Video liked`. Tap again to unlike.
- **Comment**: in the comment sheet tap `Add comment...`, type, tap the send arrow (~653, 921). Verify via the
  `Read or add comments. N comments` counter. adb `input text` breaks on `(`/`)` — avoid or escape them.
- **Delete own comment**: with the sheet open, long-press the comment → `Delete`.

## Traps
- The comment sheet shows a row of **emoji quick replies** above the input: one tap posts that emoji as a comment.
- **Long-pressing the video** (e.g. when the comment sheet has closed) opens a share menu with one-tap
  **Send** buttons to friends and **Add to Story**. Always confirm the comment sheet is open before long-pressing.
- Back closes the keyboard first, then the comment sheet.
- The screen turns off quickly: wake + `wm dismiss-keyguard` before each step.

## Automated (research.py) — 2-minute runs passed 2026-11-02 17:44–17:52
- TikTok resource ids **change between sessions** (`f62`→`f6h`, `g8m`→`g9r`): read by label/layout only.
  Stable named ids: `title` (author / creator name), `desc` (caption), `user_avatar`.
- A quick swipe (250 ms) is sometimes ignored on search-result videos; 400 ms works. Verify the creator changed.
- Close the comment sheet with its `Close` button (top right of the sheet).
- When already liked, the like button says `Video liked` with no count; the count is the number just below it.
