---
description: "Review the working notes for duplication and fragmentation, and consolidate them with approval"
agent: assistant
---
Review the active working notes.

1. Read the active notes (read_notes("notes")) and search for clusters with
   search_notes. Look for:
   - notes that say substantially the same thing;
   - one idea fragmented across several notes;
   - notes whose abstract no longer matches the body;
   - notes whose sources or evidence have moved.
2. Propose a plan: keep, delete, create, link. For each consolidation, name the
   notes to delete and the replacement note you would create, with its title,
   abstract and the ids it would link.
3. Show me the plan first. Do not delete or create anything until I approve.
4. After I approve, set the old notes to deleted with set_note_status, then create
   the replacement with save_note. In the new note's body, say "Consolidates N3,
   N7" and link the old ids. Deleted notes remain searchable with
   include_deleted=True.

$ARGUMENTS
