# orchestrate — หัวหน้าทีม AI สำหรับ Claude Code

ให้ Claude (Opus) เป็นหัวหน้า แตกงานใหญ่เป็นงานย่อย เลือกโมเดลให้แต่ละงานตามความยาก เขียน prompt ให้ลูกทีมด้วยสกิล prompt-master แล้ว**ให้คุณอนุมัติก่อน**ส่งงานจริง ระหว่างทำมีความคืบหน้าแสดงใน terminal และ dashboard สด

## ทำงานยังไง

1. **วางแผน** — lead แตกงานเป็นกราฟ งานที่ไม่ต้องรอกันรันพร้อมกัน
2. **เลือกโมเดลต่องาน** — Haiku สำหรับงานง่าย, Sonnet สำหรับงานเขียนโค้ดทั่วไป, Opus สำหรับงานยาก/เสี่ยง พร้อมเหตุผล
3. **เขียน prompt ด้วย prompt-master** + คำอธิบายภาษาไทยเข้าใจง่ายคู่ทุก prompt
4. **ขออนุมัติ** — แสดงตารางแบ่งงาน, ส่วนที่ใช้ร่วมกัน, และ prompt ทุกตัว แล้วรอคุณตอบ
5. **บังคับจริงด้วย hook** — ถ้ายังไม่อนุมัติ หรือ prompt ที่ส่งไม่ตรงกับที่อนุมัติ จะส่งงานไม่ได้
6. **ตรวจทุกงาน** — lead รันเทสต์ซ้ำเอง, hook ตรวจจาก transcript ว่ารันโมเดล/effort ตรงแผนจริง, ปิดท้ายด้วย reviewer

## ติดตั้ง

```bash
git clone https://github.com/nutfull123/claude-orchestrate.git ~/.claude/skills/orchestrate
bash ~/.claude/skills/orchestrate/install.sh
```

ต้องมี: Claude Code, Python 3, และสกิล `prompt-master`

## ใช้งาน

```
/orchestrate ทำเว็บร้านอาหารสแกนสั่ง มีหน้าลูกค้า หน้าพนักงาน และหน้าพิมพ์ QR
```

ระหว่างรัน:
- **status line** ใต้ช่องพิมพ์ — บล็อกแต่ละงาน (`▁▃▅▇` = กำลังทำ, `█` = ผ่าน) โมเดลที่ใช้จริง ✓/⚠
- **dashboard** เปิดในเบราว์เซอร์เอง — กดดูรายละเอียดแต่ละงาน: กิจกรรมสด, prompt, เหตุผล, แผน vs ใช้จริง
- `python3 ~/.claude/skills/orchestrate/orch.py watch` — panel เต็มจอในอีกแท็บ

## ไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `SKILL.md` | ขั้นตอนที่ lead ต้องทำ + คู่มือเลือกโมเดล |
| `orch.py` | ตรวจแผน, แสดง prompt, อนุมัติ, hook, status line, dashboard server |
| `dashboard.html` | หน้า dashboard |
| `agents/` | บทบาทลูกทีม: researcher (อ่านอย่างเดียว), builder (เขียนโค้ด), reviewer (ตรวจ) |
| `install.sh` | ติดตั้ง agents, hooks, status line (สำรอง settings เดิมก่อน) |

ปิด dashboard อัตโนมัติ: ตั้ง `ORCH_NO_DASHBOARD=1`
