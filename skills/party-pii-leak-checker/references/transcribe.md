# Transcription prompt (for a helper model)

Give each helper this prompt, its batch of queue items (from
`.work/<case>/queue.json`: each has an `id`, and either an `image` path or a
`pdf` path plus a `page` number), and the transcripts folder path
(`.work/<case>/transcripts/`).

---

You are transcribing pictures from an anonymized legal case file (scans, photos, images inside documents). A program will
search your transcripts for names, dates, account numbers, addresses and
other identifiers, so **exact characters matter more than anything else**.

For each item in your batch:

1. Open the image, or that page of the PDF.
2. Write everything legible on the page to `<transcripts folder>/<id>.txt`.
   The file name is the item's `id` plus `.txt`, exactly.
3. Keep the page's reading order, **one printed line per line**. Keep a table
   row on one line, its cells separated by two spaces, as printed.
4. Copy digits, letters, punctuation and spacing exactly: `528-41-9963`, not
   "an SSN". Keep masked numbers as printed (`XXXX-1234`, `****9901`).
5. Include headers, footers, stamps, form labels with their filled-in values,
   handwriting you can read, and anything in the margins.
6. Write `[illegible]` for anything you cannot read with confidence. **Never
   guess a digit or a letter**: a wrong character is worse than a gap,
   because a wrong account number cannot be matched. If part of a number is
   readable, write the readable part and `[illegible]` for the rest
   (`4417 [illegible] 9301`).
7. Do not summarize, translate, correct spelling, redact, reorder or comment.
   Do not add headings of your own.
8. If the page is blank or unreadable, still write the file: an empty file for
   a blank page, or `[illegible]` for an unreadable one. The run waits for
   every file.
9. If the image is small or blurry, zoom in or view it at full size before
   reading long numbers.

When the batch is done, reply only with the ids you wrote. Do not repeat
any page contents in your reply.
