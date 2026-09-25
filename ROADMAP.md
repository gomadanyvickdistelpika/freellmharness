# Roadmap and known issues

AEGIS is an early test version. Help is welcome on anything here — look for the matching
issue or open one.

## Known issues / rough edges

- Tested mainly on one Windows 11 laptop (16 GB RAM, no GPU). Other setups need testers.
- Live behaviour of some free providers (xKiro, 9Router, Pollinations, Atria) is matched by
  pattern; new error wordings may need adding.
- Computer use and the Windows offline voice are only lightly tested on real desktops.
- The optional Needle fast-command step is English-only for now.
- No installer `.exe` yet — setup needs Python.

## Next

- [ ] Screenshots / short demo GIF in the README
- [ ] Clean-machine testing of `AEGIS-SETUP.bat` on Windows 10 and 11
- [ ] A one-file Windows installer (PyInstaller or similar)
- [ ] `run.sh` launcher for Linux/macOS development
- [ ] French (and other) translations of the UI and README
- [ ] Needle fast commands in other languages
- [ ] More "Me" templates and skills contributed by the community

## Later

- Deeper subagent trees
- A plugin format for sharing skills and agents
- Optional sync of settings between PCs (opt-in, encrypted)
