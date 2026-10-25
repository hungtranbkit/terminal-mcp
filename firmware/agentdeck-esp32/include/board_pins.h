#pragma once
// CANDIDATE ES3C28P: verify board revision before flashing. Not CYD.
constexpr int LCD_CS=10, LCD_DC=46, LCD_SCK=12, LCD_MOSI=11, LCD_MISO=13, LCD_BL=45;
constexpr int TOUCH_SDA=16, TOUCH_SCL=15, TOUCH_RST=18, TOUCH_INT=17;
static_assert(LCD_DC==46 && LCD_BL==45 && TOUCH_SDA==16,"ES3C28P profile required");
