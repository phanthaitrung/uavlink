# Text style for every user-facing string

Applies to the setup page, `share/schema.json`, `share/presets/`, `share/i18n/` and any later user-facing text.

The owner's request (Vietnamese, verbatim): "nội dung cần ghi văn phong chính thống, đề mục không đặt câu hỏi mà ghi câu
khẳng định vào đối tượng chính của nội dung. Ví dụ hỏi "Máy này làm nhiệm vụ gì?" thì ghi "Chọn nhiệm vụ của máy".
Đối với đề mục có con số, thì ghi số chứ không ghi chữ."

1. **Formal register** (văn phong hành chính – kỹ thuật). Vietnamese: no "bạn", "chúng tôi", "mình"; no casual "hãy …
   nhé", "ổn", "xịn", no exclamation marks, no emoji. Use impersonal statements ("Hệ thống chưa thay đổi cho tới khi
   bấm Áp dụng.") or short direct instructions ("Chọn…", "Nhập…", "Kiểm tra…", "Tháo cánh quạt trước khi…").
   English: same tone; no "we"; "you" only where unavoidable; plain imperatives.
2. **Headings are statements about the main object, never questions.** Applies to step titles, section titles, card
   titles, tab names, table headers, group labels, dialog titles and confirmation prompts.
   "Máy này làm nhiệm vụ gì?" → "Chọn nhiệm vụ của máy"; "Đóng trang …?" → "Xác nhận đóng trang …".
   English: "What does this machine do?" → "Select the machine role".
3. **Numbers in digits**, never in words: "Bước 5", "1. Chọn nhiệm vụ của máy", "12 ký tự", "30 phút".
4. **Glossary** (use exactly these terms, both languages):

   | Concept | Tiếng Việt | English |
   |---|---|---|
   | preset | mẫu cấu hình | preset |
   | the setup page / its server | trang cấu hình / máy chủ trang cấu hình | setup page / setup server |
   | flight controller (CUAV X25 EVO) | mạch bay X25 | X25 flight controller |
   | companion computer (CM5) | máy tính đồng hành CM5 | CM5 companion computer |
   | VPS relay | máy chủ chuyển tiếp (VPS) | relay server (VPS) |
   | ground control station | trạm điều khiển mặt đất (GCS) | ground control station (GCS) |
   | MAVLink2 signing | ký MAVLink2 / passphrase ký | MAVLink2 signing / signing passphrase |
   | pre-check (step 4) | kiểm tra trước | pre-check |
   | apply | áp dụng | apply |
   | rollback timer | hẹn giờ tự hoàn tác | automatic rollback timer |
   | armed / disarmed | đang ARM / đã DISARM | armed / disarmed |
   | firewall | tường lửa | firewall |
   | propellers | cánh quạt | propellers |

5. **Keep the data shape**: same JSON keys, same `{placeholders}`, same structure; change only the text values. Keep
   texts about as long as now (they must fit the cards and table cells). Both `vi` and `en` must say the same thing.
6. Technical tokens stay as they are: IPs, ports, file paths, unit names, parameter names (`NET_P1_PORT`), command lines,
   product names (Mission Planner, QGroundControl, Tailscale, Starlink, ArduPilot).
