# Third-party references and notices

The local probes and reader adapt or cross-check portions of these projects:

- **huohuoer/wechat-cli**, commit `a3789232d4f79bf0b30634d9dadbce71e4acd601`, Apache-2.0. Page layout, key verification and Windows read-only scan references. Source: https://github.com/huohuoer/wechat-cli . License retained in `licenses/wechat-cli-Apache-2.0.txt`.
- **TANGandXUE/wcdb-key-tool**, commit `79f1b5b92e12c66aa281b4a60a3c478b5f547dfa`, MIT, Copyright (c) 2025 CloudDreamAI / TANGandXUE. The Config.Cipher object scan in `tools/verify_wechat_contacts.py` adapts the read-only Windows extraction approach. Source: https://github.com/TANGandXUE/wcdb-key-tool . License retained in `licenses/wcdb-key-tool-MIT.txt`.

Modifications in this workspace: bounded reads, explicit Windows API signatures, active-account binding, per-page authentication, no key logging or persistence, authenticated in-memory SQLite snapshots, committed-WAL validation, cancellation, memory cache, contact/message queries and GUI. The original CLIs are not invoked.

The WAL parser was independently implemented against the SQLite WAL file-format documentation and cross-checked with SQLCipher's WAL codec placement. Synthetic tests cover salt/checksum failure and commit boundaries.

`Hello-Mr-Crab/pywechat` was inspected for remark UI behavior at commit `ebe8d4be9347816d221c38843e5b9e5e480bd8fb`. It is not imported, installed or bundled into the running application; automatic remark writing is not implemented.

Runtime dependencies retain their own licenses: PySide6/Qt, psutil, PyCryptodome, zstandard and Python. See the installed distributions' license files.
