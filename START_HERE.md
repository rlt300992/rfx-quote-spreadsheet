# Start here

1. Unzip this folder somewhere, e.g. `~/rfx-copilot`.
2. In that folder, create a file named `.env` with these two lines, then save it:
   ```
   GEMINI_API_KEY=your-key-from-aistudio.google.com/app/apikey
   APP_PASSCODE=choose-any-passcode
   ```
3. Open a terminal in the folder and run `claude` to start Claude Code.
4. Open `PROMPTS.md`, copy the **P0** block, paste it into Claude Code, and press Enter.
5. After each phase, do the **"You check"** steps before pasting the next phase.

**Files in this kit**
- `CLAUDE.md`: the project brief, rules and decisions. Claude Code reads it automatically.
- `docs/SCHEMA.md`: the full database design.
- `data/`: seed data, the 5 vendor replies plus attachments, the answer key, and the scripts that generated them.
- `PROMPTS.md`: the 7 phases (P0–P6).

**Never** paste your Gemini key into a chat, and never commit `.env`.
