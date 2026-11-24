---
description: Ghép máy này với crew-hub bằng mã dùng một lần từ /crew pair trên Slack
argument-hint: <mã> <hub> [tên-máy]
---
Gọi tool `crew_pair` của MCP server `crew`. Tham số lấy từ: `$ARGUMENTS`, theo thứ tự mã ghép (dạng ABCD-EFGH), địa chỉ hub (bắt đầu bằng http:// hoặc https://), và tên máy nếu có.

Thiếu mã hoặc địa chỉ hub thì không gọi tool, chỉ nhắc người dùng gõ `/crew pair` trên Slack rồi dán nguyên dòng nó đưa. Nếu tool báo máy đang ghép với một hub khác, hỏi người dùng có muốn chuyển không; chỉ gọi lại với `replace` là true khi họ đồng ý (mã ghép vẫn còn dùng được vì lần trước chưa tới hub). Báo lại kết quả của tool thật ngắn, không nhắc lại mã ghép.
