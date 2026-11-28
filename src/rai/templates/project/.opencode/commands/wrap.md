---
description: "Record what's unrecorded, summarise this conversation, and ask whether anything should be remembered"
---
We're ending this conversation. Do three things.

1. **Record what's unrecorded.** For anything I decided, dismissed, deferred or said I
   had read that you haven't already recorded with the tools (resolve_objection,
   record_decision, set_reading_status, decide_candidate), record it now, in my words.
   Don't record your own suggestions as decisions.

2. **Summarise the conversation** with record_session, from this conversation only:
   - discussed: three to eight plain sentences on what we covered, in the order it came
     up, using ids (O12, C3, D4) and report paths where they apply.
   - understandings: the new understandings that arose, one sentence each: something we
     came to see that we didn't at the start (a connection, a correction, a reason).
     Say whose it was, mine or yours. These are not decisions. Leave it empty if there
     were none.
   - left_off: where we stopped: the thread we were in the middle of, what I said I'd do
     next, and any open question. One short paragraph.
   - reports: the files you saved in reports/ during this conversation.
   - notes: any working-note ids (N1...) created or revised during this conversation.
   - agent: your own name.

3. **Ask whether anything should be remembered.** Before you close, ask me: "Anything
   to remember before we stop?" Do not create a working note unless I say yes or tell
   you what to remember. If I do, search the existing notes first and then record or
   revise them with save_note/save_notes or revise_note. Working notes are prior
   thinking, not decisions. If you are Sparring, you cannot write notes: say so and
   let me switch to the assistant for that part.

Then show me the summary in the chat in those three parts, in at most twelve lines, and
ask me to correct anything that's wrong. If I do, call record_session again with
replaces set to the earlier summary's id. If I asked you to remember something in step
3, call record_session again with replaces set to the earlier summary's id and include
the note ids you just created or revised. $ARGUMENTS
