# Phân tích cấu trúc `server/dashboard.py` (11,512 dòng)

## 1. Tổng quan

File `server/dashboard.py` là một monolith Flask application chứa **toàn bộ** logic của JupyterHub Multi-User Dashboard:
- 15 core functions
- 35+ HTML template variables
- 90+ Flask routes
- 30+ SocketIO event handlers
- 7 MongoDB collection init helpers

---

## 2. Danh sách đầy đủ theo nhóm chức năng

### [A] Core / Config (~80 dòng, 0.7%)
| Dòng | Tên | Mô tả |
|------|-----|--------|
| 45 | `app = Flask(...)` | Flask app instance |
| 49 | `socketio = SocketIO(...)` | SocketIO instance |
| 52-66 | Config vars | ONLYOFFICE_URL, ADMIN_USER, MONGO_* |
| 71 | `get_db()` | MongoDB lazy connection |

### [B] User & System Management (~100 dòng, 0.9%)
| Dòng | Tên | Mô tả |
|------|-----|--------|
| 83 | `generate_password()` | Tạo mật khẩu ngẫu nhiên |
| 88 | `get_users()` | Lấy danh sách user hệ thống |
| 96 | `get_usernames()` | Lấy danh sách tên user |
| 100 | `user_exists()` | Kiểm tra user (bản 1 - bị ghi đè) |
| 112 | `set_user_password()` | Đặt mật khẩu hệ thống |
| 117 | `regenerate_nginx()` | Tái tạo nginx config |
| 121 | `create_system_user()` | Tạo user Linux |
| 132 | `delete_system_user()` | Xóa user Linux |
| 140 | `user_exists()` | Kiểm tra user (bản 2 - TRÙNG TÊN!) |
| 148 | `check_user_auth()` | Xác thực PAM |

### [C] Jupyter Process Management (~30 dòng, 0.3%)
| Dòng | Tên | Mô tả |
|------|-----|--------|
| 104 | `get_user_port()` | Tính port theo UID |
| 153 | `start_jupyter()` | Khởi động JupyterLab |
| 173 | `stop_jupyter()` | Dừng JupyterLab |
| 177 | `is_jupyter_running()` | Kiểm tra trạng thái |

### [D] HTML Templates (~6,630 dòng, 57.6%)

#### CSS chung
| Dòng | Biến | Mô tả |
|------|------|--------|
| 186 | `CSS` | CSS chung toàn trang |
| 2126 | `EMBED_CSS` | CSS cho embed iframes |
| 6506 | `VIEWER_BASE_CSS` | CSS cho file viewer |

#### Auth templates
| Dòng | Biến |
|------|------|
| 263 | `LOGIN_PAGE` |
| 281 | `CHANGE_PW` |
| 782 | `USER_CHANGE_PW` |
| 6244 | `EMBED_CHANGE_PW` |

#### Admin templates
| Dòng | Biến |
|------|------|
| 298 | `ADMIN_DASH` |
| 805 | `ADMIN_S3_CONFIG` |
| 861 | `ADMIN_EXTENSIONS` |

#### User Desktop
| Dòng | Biến |
|------|------|
| 327 | `USER_MENU` (~435 dòng) |
| 763 | `USER_LAB` |

#### S3/Backup templates
| Dòng | Biến |
|------|------|
| 1097 | `USER_S3_CONFIG` |
| 1155 | `S3_BACKUP_PAGE` (~380 dòng) |
| 2289 | `EMBED_S3_BACKUP` |
| 6183 | `EMBED_S3_CONFIG` |

#### Shared Space templates
| Dòng | Biến |
|------|------|
| 1543 | `SHARED_SPACE_NO_CONFIG` |
| 1559 | `SHARED_SPACE_PAGE` (~340 dòng) |
| 2393 | `EMBED_SHARED_SPACE` |

#### Share Link templates
| Dòng | Biến |
|------|------|
| 1909 | `SHARE_PASSWORD_PAGE` |
| 1925 | `SHARE_FILE_PAGE` |
| 1974 | `SHARE_FOLDER_PAGE` |
| 2051 | `SHARE_NOT_FOUND` |
| 2060 | `SHARE_EXPIRED` |
| 2069 | `MY_SHARES_PAGE` |
| 2455 | `EMBED_MY_SHARES` |

#### Embed templates (for desktop UI iframes)
| Dòng | Biến | Kích thước |
|------|------|-----------|
| 2265 | `EMBED_LAB` | nhỏ |
| 2489 | `EMBED_WORKSPACE` | nhỏ |
| 2532 | `EMBED_BROWSER` | ~70 dòng |
| 2603 | `EMBED_CHAT` | ~1,000 dòng |
| 3611 | `EMBED_TODO` | ~1,140 dòng |
| 4757 | `EMBED_MUSIC_ROOM` | ~630 dòng |
| 5390 | `EMBED_SCREEN_SHARE` | ~520 dòng |
| 5911 | `EMBED_SCREEN_GUEST` | ~200 dòng |
| 6118 | `EMBED_USER_SHARES` | nhỏ |
| 6265 | `EMBED_GAME_HUB` | ~240 dòng |

#### File Viewer templates
| Dòng | Biến |
|------|------|
| 6520 | `VIEWER_IMAGE` |
| 6557 | `VIEWER_VIDEO` |
| 6578 | `VIEWER_AUDIO` |
| 6604 | `VIEWER_TEXT` |
| 6639 | `VIEWER_MARKDOWN` |
| 6685 | `VIEWER_HTML` |
| 6716 | `VIEWER_PDF` |
| 6734 | `VIEWER_OFFICE` |
| 6784 | `VIEWER_UNSUPPORTED` |

### [E] Auth & Page Routes (~250 dòng, 2.2%)
| Dòng | Route | Function |
|------|-------|----------|
| 6816 | `GET/POST /` | `login()` |
| 6830 | `GET /dashboard` | `dashboard()` |
| 6850 | `GET /lab` | `lab()` |
| 6863-6951 | `GET /embed/*` | 15 embed routes |
| 6963 | `GET /balatro/` | `balatro_index()` |
| 7000 | `GET/POST /embed/change-password` | `embed_change_password()` |
| 7023 | `GET/POST /user/change-password` | `user_change_password()` |
| 7071 | `GET/POST /change-password` | `change_password()` |
| 7086 | `GET /logout` | `logout()` |

### [F] Admin API (~100 dòng, 0.9%)
| Dòng | Route | Function |
|------|-------|----------|
| 7040 | `POST /admin/create` | `admin_create()` |
| 7053 | `POST /admin/reset` | `admin_reset()` |
| 7062 | `POST /admin/delete` | `admin_delete()` |
| 7098-7135 | `/admin/extensions/*` | Extension management |
| 7146-7171 | `/admin/s3-config/*` | S3 config management |

### [G] S3/Workspace File API (~270 dòng, 2.3%)
| Dòng | Route | Function |
|------|-------|----------|
| 7261 | `GET /api/workspace/list` | `api_ws_list()` |
| 7271 | `GET /api/s3/list` | `api_s3_list()` |
| 7289 | `POST /api/transfer` | `api_transfer()` |
| 7319 | `GET /api/transfer/status/<id>` | `api_transfer_status()` |
| 7327 | `POST /api/workspace/mkdir` | `api_ws_mkdir()` |
| 7336 | `POST /api/s3/mkdir` | `api_s3_mkdir()` |
| 7355 | `POST /api/workspace/delete` | `api_ws_delete()` |
| 7365 | `POST /api/s3/delete` | `api_s3_delete()` |
| 7385 | `POST /api/s3/move` | `api_s3_move()` |
| 7416 | `POST /api/workspace/upload` | `api_ws_upload()` |
| 7432 | `POST /api/workspace/fix-permissions` | `api_ws_fix_permissions()` |
| 7498 | `POST /api/s3/upload` | `api_s3_upload()` |

### [H] Shared Space API (~110 dòng, 1.0%)
| Dòng | Route | Function |
|------|-------|----------|
| 7540 | `GET /api/shared/list` | `api_shared_list()` |
| 7557 | `POST /api/shared/mkdir` | `api_shared_mkdir()` |
| 7575 | `POST /api/shared/delete` | `api_shared_delete()` |
| 7594 | `POST /api/shared/transfer` | `api_shared_transfer()` |
| 7624 | `POST /api/shared/upload` | `api_shared_upload()` |

### [I] User-to-User Shares (~280 dòng, 2.4%)
| Dòng | Route | Function |
|------|-------|----------|
| 7651 | helper | `_init_user_shares_collection()` |
| 7660 | helper | `_init_notifications_collection()` |
| 7668 | `GET /api/users/search` | `api_users_search()` |
| 7688 | `POST /api/share-with-user` | `api_share_with_user()` |
| 7770 | `GET /api/user-shares/incoming` | `api_user_shares_incoming()` |
| 7791 | `GET /api/user-shares/sent` | `api_user_shares_sent()` |
| 7812 | `POST /api/user-shares/accept` | `api_user_shares_accept()` |
| 7854 | `POST /api/user-shares/reject` | `api_user_shares_reject()` |
| 7876 | `GET /api/notifications` | `api_notifications()` |
| 7898 | `POST /api/notifications/mark-read` | `api_notifications_mark_read()` |

### [J] Share Links (Public) (~300 dòng, 2.6%)
| Dòng | Route | Function |
|------|-------|----------|
| 7929 | helper | `_init_shared_links_collection()` |
| 7936 | helper | `_format_size()` |
| 7943 | `GET/POST /share/<id>` | `share_public()` |
| 8016 | `GET /share/<id>/download` | `share_download()` |
| 8068 | `GET /share/<id>/download/zip` | `share_download_zip()` |
| 8106 | `POST /api/share/create` | `api_share_create()` |
| 8171 | `GET /api/share/list` | `api_share_list()` |
| 8193 | `POST /api/share/delete` | `api_share_delete()` |
| 8213 | `GET /my-shares` | `my_shares()` |

### [K] OnlyOffice & File Viewer (~500 dòng, 4.3%)
| Dòng | Route | Function |
|------|-------|----------|
| 8258 | helper | `get_file_type()` |
| 8267 | helper | `generate_onlyoffice_token()` |
| 8278 | helper | `verify_onlyoffice_token()` |
| 8289 | `GET /api/onlyoffice/file` | `onlyoffice_file_stream()` |
| 8353 | `POST /api/onlyoffice/callback` | `onlyoffice_callback()` |
| 8448 | `GET /api/workspace/file` | `workspace_file_stream()` |
| 8467 | `GET /api/workspace/download` | `workspace_file_download()` |
| 8486 | `GET /api/s3/file` | `s3_file_stream()` |
| 8512 | `GET /api/s3/download` | `s3_file_download()` |
| 8538 | `GET /api/shared/file` | `shared_file_stream()` |
| 8563 | `GET /api/shared/download` | `shared_file_download()` |
| 8588 | `GET /viewer/<source>` | `file_viewer()` |

### [L] Chat System (~1,200 dòng, 10.4%)

#### SocketIO handlers
| Dòng | Event | Function |
|------|-------|----------|
| 8756 | helper | `_init_messages_collection()` |
| 8765 | helper | `_init_pending_files_collection()` |
| 8773 | `connect` | `handle_connect()` |
| 8797 | `disconnect` | `handle_disconnect()` |
| 8812 | `get_online_users` | `handle_get_online_users()` |
| 8826 | `send_message` | `handle_send_message()` |
| 8876 | `send_file` | `handle_send_file()` |
| 8948 | `accept_file` | `handle_accept_file()` |
| 8997 | `reject_file` | `handle_reject_file()` |
| 9027 | `get_messages` | `handle_get_messages()` |
| 9066 | `mark_messages_read` | `handle_mark_messages_read()` |

#### REST API
| Dòng | Route | Function |
|------|-------|----------|
| 9089 | `GET /api/chat/users` | `api_chat_users()` |
| 9116 | `GET /api/chat/pending-files` | `api_chat_pending_files()` |
| 9362 | `GET /api/chat/contacts` | `api_chat_contacts()` |
| 9465 | `POST /api/chat/upload` | `api_chat_upload()` |
| 9570 | `GET /api/chat/file/<id>` | `api_chat_file_download()` |
| 9620 | `POST /api/chat/file/accept` | `api_chat_file_accept()` |
| 9665 | `POST /api/chat/file/reject` | `api_chat_file_reject()` |
| 9710 | helper | `find_chat_file_in_s3()` |
| 9768 | `POST /api/chat/file/save` | `api_chat_file_save()` |
| 9835 | `POST /api/chat/file-to-workspace` | `api_chat_file_to_workspace()` |
| 9881 | `POST /api/chat/message/recall` | `api_chat_message_recall()` |

#### Friends API
| Dòng | Route | Function |
|------|-------|----------|
| 9146 | helper | `_init_friends_collection()` |
| 9155 | `GET /api/friends/list` | `api_friends_list()` |
| 9199 | `GET /api/friends/search` | `api_friends_search()` |
| 9227 | `POST /api/friends/add` | `api_friends_add()` |
| 9288 | `POST /api/friends/accept` | `api_friends_accept()` |
| 9314 | `POST /api/friends/reject` | `api_friends_reject()` |
| 9334 | `POST /api/friends/remove` | `api_friends_remove()` |

### [M] Todo/Notes API (~380 dòng, 3.3%)
| Dòng | Route | Function |
|------|-------|----------|
| 9964 | helper | `_init_todos_collection()` |
| 9973 | `GET /api/todos` | `api_todos_list()` |
| 10057 | `POST /api/todos` | `api_todos_create()` |
| 10114 | `GET /api/todos/<id>` | `api_todos_get()` |
| 10147 | `PUT /api/todos/<id>` | `api_todos_update()` |
| 10203 | `DELETE /api/todos/<id>` | `api_todos_delete()` |
| 10218 | `PUT /api/todos/<id>/status` | `api_todos_status()` |
| 10255 | `POST /api/todos/<id>/comment` | `api_todos_comment()` |
| 10304 | `GET /api/todos/users` | `api_todos_users()` |

### [N] Music Room (~760 dòng, 6.6%)

#### REST API
| Dòng | Route | Function |
|------|-------|----------|
| 10341 | helper | `_init_music_rooms_collection()` |
| 10349 | `GET /api/music/rooms` | `api_music_rooms()` |
| 10371 | `POST /api/music/upload` | `api_music_upload()` |
| 10410 | `GET /api/music/stream/<key>` | `api_music_stream()` |
| 10440 | `GET /api/music/s3-audio` | `api_music_s3_audio()` |

#### SocketIO handlers
| Dòng | Event | Function |
|------|-------|----------|
| 10464 | `create_music_room` | `handle_create_music_room()` |
| 10521 | `join_music_room` | `handle_join_music_room()` |
| 10577 | `leave_music_room` | `handle_leave_music_room()` |
| 10628 | `music_play` | `handle_music_play()` |
| 10674 | `music_pause` | `handle_music_pause()` |
| 10716 | `music_seek` | `handle_music_seek()` |
| 10745 | `music_next` | `handle_music_next()` |
| 10811 | `music_prev` | `handle_music_prev()` |
| 10856 | `music_shuffle` | `handle_music_shuffle()` |
| 10899 | `music_repeat` | `handle_music_repeat()` |
| 10942 | `add_track` | `handle_add_track()` |
| 10988 | `remove_track` | `handle_remove_track()` |
| 11034 | `import_from_s3` | `handle_import_from_s3()` |

### [O] Screen Share (~430 dòng, 3.7%)

#### REST API
| Dòng | Route | Function |
|------|-------|----------|
| 11091 | helper | `_init_screen_sessions_collection()` |
| 11098 | `GET /api/screen/sessions` | `api_screen_sessions()` |
| 11123 | `GET /api/screen/session/<id>` | `api_screen_session()` |
| 11145 | `POST /api/screen/verify-password` | `api_screen_verify_password()` |
| 11169 | helper | `generate_screen_code()` |

#### SocketIO handlers
| Dòng | Event | Function |
|------|-------|----------|
| 11175 | `start_screen_share` | `handle_start_screen_share()` |
| 11218 | `stop_screen_share` | `handle_stop_screen_share()` |
| 11238 | `delete_screen_session` | `handle_delete_screen_session()` |
| 11258 | `join_screen_session` | `handle_join_screen_session()` |
| 11307 | `join_screen_by_code` | `handle_join_screen_by_code()` |
| 11359 | `leave_screen_session` | `handle_leave_screen_session()` |
| 11387 | `webrtc_offer` | `handle_webrtc_offer()` |
| 11412 | `webrtc_answer` | `handle_webrtc_answer()` |
| 11436 | `webrtc_ice` | `handle_webrtc_ice()` |
| 11469 | `screen_chat` | `handle_screen_chat()` |

---

## 3. Dependency Map

```
                    ┌───────────┐
                    │ [A] Core  │ ← get_db(), app, socketio
                    │  Config   │
                    └─────┬─────┘
                          │ (mọi nhóm đều phụ thuộc)
           ┌──────────────┼──────────────────┐
           │              │                  │
     ┌─────▼─────┐  ┌────▼─────┐     ┌──────▼──────┐
     │[B] User & │  │[C]Jupyter│     │[D] Templates│
     │ System Mgmt│  │ Process  │     │  (57% file) │
     └─────┬─────┘  └────┬─────┘     └──────┬──────┘
           │              │                  │
     ┌─────▼──────────────▼──────┐           │
     │  [E] Auth & Page Routes   │←──────────┘
     │  (dùng templates + auth)  │   (render_template_string)
     └─────┬─────────────────────┘
           │
     ┌─────▼─────┐
     │ [F] Admin  │──→ [B] create/delete user
     │   API      │──→ [C] start/stop jupyter
     └───────────┘──→ extension_manager (external)
           │
     ┌─────▼──────────────────────────────────────┐
     │        External: s3_manager module          │
     │  (S3 operations, shared config, streaming)  │
     └─────────────────────┬──────────────────────┘
           ┌───────────────┼────────────────┐
     ┌─────▼─────┐  ┌─────▼─────┐  ┌───────▼──────┐
     │[G] S3/WS  │  │[H] Shared │  │ [J] Share    │
     │ File API  │  │ Space API │  │ Links        │
     └───────────┘  └───────────┘  └──────────────┘
                                         │
     ┌───────────────────────────────────┘
     │
     ┌─────▼─────┐         ┌───────────┐
     │[I] User   │────────→│ [A] get_db│
     │ Shares    │         └───────────┘
     └───────────┘

     ┌───────────────┐
     │[K] OnlyOffice │──→ s3_manager (stream files)
     │ & File Viewer │──→ jwt (token generation)
     └───────────────┘

     ┌────────────────────────────────────┐
     │       REALTIME (SocketIO)          │
     ├────────────┬──────────┬────────────┤
     │[L] Chat    │[N] Music │[O] Screen  │
     │ + Friends  │  Room    │   Share     │
     └──────┬─────┴────┬─────┴─────┬──────┘
            │          │           │
            ▼          ▼           ▼
        get_db()   s3_manager   WebRTC signaling

     ┌──────────┐
     │[M] Todo  │──→ get_db()
     │  /Notes  │
     └──────────┘
```

### Dependency Summary

| Nhóm | Phụ thuộc vào |
|------|---------------|
| [A] Core | Flask, SocketIO, MongoDB, env vars |
| [B] User Mgmt | [A], subprocess, pam, pwd |
| [C] Jupyter | [A], subprocess, socket |
| [D] Templates | Thuần HTML/CSS/JS (không import gì) |
| [E] Auth Routes | [A], [B], [C], [D] |
| [F] Admin | [A], [B], [C], extension_manager |
| [G] S3/WS API | [A], s3_manager |
| [H] Shared Space | [A], s3_manager |
| [I] User Shares | [A], s3_manager |
| [J] Share Links | [A], s3_manager |
| [K] OnlyOffice | [A], s3_manager, jwt |
| [L] Chat | [A], s3_manager, SocketIO |
| [M] Todo | [A] |
| [N] Music | [A], s3_manager, SocketIO |
| [O] Screen Share | [A], SocketIO (WebRTC signaling) |

---

## 4. Đề xuất tách file/folder

```
server/
├── app.py                          # Flask app factory, config, get_db() (~80 dòng)
│
├── utils/
│   ├── __init__.py
│   ├── user_mgmt.py                # [B] get_users, create/delete_system_user,
│   │                               #     set_user_password, check_user_auth (~70 dòng)
│   ├── jupyter_mgmt.py             # [C] start/stop/is_running_jupyter,
│   │                               #     get_user_port (~30 dòng)
│   └── helpers.py                  # generate_password, _format_size,
│                                   #     get_file_type (~30 dòng)
│
├── templates/                      # [D] (~6,630 dòng → chuyển sang .html files)
│   ├── css.py                      # CSS, EMBED_CSS, VIEWER_BASE_CSS
│   ├── auth.py                     # LOGIN_PAGE, CHANGE_PW, EMBED_CHANGE_PW
│   ├── admin.py                    # ADMIN_DASH, ADMIN_S3_CONFIG, ADMIN_EXTENSIONS
│   ├── user_desktop.py             # USER_MENU, USER_LAB (~500 dòng)
│   ├── s3_backup.py                # S3_BACKUP_PAGE, USER_S3_CONFIG, EMBED_S3_*
│   ├── shared_space.py             # SHARED_SPACE_PAGE, EMBED_SHARED_SPACE
│   ├── share_links.py              # SHARE_*_PAGE, MY_SHARES_PAGE, EMBED_MY_SHARES
│   ├── workspace.py                # EMBED_WORKSPACE
│   ├── user_shares.py              # EMBED_USER_SHARES
│   ├── chat.py                     # EMBED_CHAT (~1,000 dòng)
│   ├── todo.py                     # EMBED_TODO (~1,140 dòng)
│   ├── music_room.py               # EMBED_MUSIC_ROOM (~630 dòng)
│   ├── screen_share.py             # EMBED_SCREEN_SHARE + GUEST (~720 dòng)
│   ├── game_hub.py                 # EMBED_GAME_HUB (~240 dòng)
│   ├── browser.py                  # EMBED_BROWSER, EMBED_LAB
│   └── viewers.py                  # VIEWER_* (9 templates, ~310 dòng)
│
├── routes/                         # Flask Blueprints (~3,100 dòng)
│   ├── __init__.py                 # register_blueprints()
│   ├── auth.py                     # [E] login, logout, dashboard, embed routes
│   ├── admin.py                    # [F] admin_create/reset/delete, extensions, s3
│   ├── workspace_api.py            # [G] api_ws_*, api_s3_*, api_transfer*
│   ├── shared_space_api.py         # [H] api_shared_*
│   ├── user_shares_api.py          # [I] api_share_with_user, api_user_shares_*
│   ├── share_links_api.py          # [J] share_public, share_download, api_share_*
│   ├── file_viewer.py              # [K] onlyoffice_*, file_viewer, file streams
│   ├── chat_api.py                 # [L REST] api_chat_*, api_friends_*
│   ├── todo_api.py                 # [M] api_todos_*
│   ├── music_api.py                # [N REST] api_music_*
│   └── screen_api.py               # [O REST] api_screen_*
│
├── realtime/                       # SocketIO handlers (~1,600 dòng)
│   ├── __init__.py
│   ├── chat_handlers.py            # [L] connect/disconnect, send_message, etc.
│   ├── music_handlers.py           # [N] music room socket events
│   └── screen_handlers.py          # [O] screen share + WebRTC signaling
│
├── dashboard.py                    # Entry point: import all, socketio.run() (~20 dòng)
├── extension_manager.py            # (đã tách sẵn)
└── s3_manager.py                   # (đã tách sẵn)
```

### Phân bổ sau khi tách

| Thư mục | Số dòng ước tính | % |
|---------|----------------:|----:|
| `templates/` | ~6,630 | 57.6% |
| `routes/` | ~3,100 | 26.9% |
| `realtime/` | ~1,600 | 13.9% |
| `utils/` | ~130 | 1.1% |
| `app.py` + `dashboard.py` | ~100 | 0.9% |

---

## 5. Vấn đề cần lưu ý

1. **Function trùng tên**: `user_exists()` được định nghĩa 2 lần (dòng 100 và 140). Bản ở dòng 140 ghi đè bản 100.
2. **Templates inline**: 57.6% file là HTML string. Nên chuyển sang Jinja2 `.html` files thay vì `render_template_string`.
3. **Không có Blueprint**: Tất cả routes đăng ký trực tiếp trên `app`. Cần refactor sang Flask Blueprint.
4. **SocketIO handlers mixed**: Chat, Music, Screen share đều đăng ký trên cùng 1 `socketio` instance. Nên tách ra namespace hoặc file riêng.
5. **Ưu tiên tách**: Templates > Routes > Realtime > Utils (theo kích thước và độ phức tạp).
