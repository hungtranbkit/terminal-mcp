#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_ILI9341.h>
#include <time.h>
#include "board_pins.h"
#if __has_include("config.local.h")
#include "config.local.h"
#else
#include "config.example.h"
#endif

Adafruit_ILI9341 lcd(LCD_CS,LCD_DC,-1);
struct Item { String id, machine, session, status; int age=-1; };
struct Snapshot { Item items[6]; int count=0, running=0, total=0, next=-1; };
Snapshot data;
SemaphoreHandle_t lock;
TaskHandle_t worker;
bool demo=DECK_BOOT_DEMO, filtered=false, busy=false, valid=false;
int page=0, selectedCard=-1;
uint32_t generation=0, lastGood=0, lastAttempt=0, lastTouch=0;
String network="DEMO";
constexpr uint16_t BG=0x0863, FG=0xE73C, MUTED=0x8410;
uint16_t color(const String &s) {
  if(s=="working") return 0x07E0;
  if(s=="done") return 0x07FF;
  if(s=="waiting" || s=="pending") return 0xFFE0;
  if(s=="failed" || s=="blocked") return 0xF800;
  return MUTED;
}
void text(int x,int y,const String &s,uint16_t c=FG,int max=18) {
  lcd.setCursor(x,y); lcd.setTextColor(c); lcd.print(s.substring(0,max));
}
// Six original 8x8 pixel creatures, generated from fixed seeds and silhouettes.
void avatar(int x,int y,int seed,uint16_t c) {
  for(int r=0;r<8;r++) for(int col=0;col<8;col++) {
    int mirror=col<4?col:7-col;
    bool body=r>=2 && r<=6 && mirror>=((r==2||r==6)?1:0);
    bool ears=r<2 && mirror==((seed%3)+1);
    bool feet=r==7 && (mirror==1 || mirror==3);
    if(body||ears||feet) lcd.fillRect(x+col*3,y+r*3,3,3,c);
  }
  lcd.fillRect(x+6,y+9,3,3,BG); lcd.fillRect(x+15,y+9,3,3,BG);
  if(seed%2) lcd.fillRect(x+9,y+18,6,3,BG);
}
void makeDemo() {
  data=Snapshot(); data.total=6; data.running=2; data.next=-1;
  const char *states[]={"working","waiting","done","working","blocked","unknown"};
  for(int i=0;i<6;i++) {
    if(filtered && String(states[i])!="working") continue;
    Item &a=data.items[data.count++]; a.id=String(i); a.machine=i<3?"Desk":"Lab";
    a.session="Agent "+String(i+1); a.status=states[i]; a.age=0;
  }
  valid=true;
}
void draw() {
  xSemaphoreTake(lock,portMAX_DELAY);
  lcd.fillScreen(BG); lcd.setTextSize(1); lcd.setTextWrap(false);
  text(6,5,"AGENTS DECK",0x07FF);
  text(6,19,String(data.running)+" running / "+String(data.total)+" all");
  bool stale=!demo && valid && uint32_t(millis()-lastGood)>90000;
  text(6,33,demo?"DEMO offline":(stale?"STALE | "+network:network),stale?0xFFE0:MUTED,37);
  if(selectedCard>=0 && selectedCard<data.count) {
    Item &a=data.items[selectedCard]; avatar(100,65,selectedCard,color(a.status));
    text(12,110,a.machine,FG,24); text(12,130,a.session,FG,24);
    text(12,155,stale?"stale":a.status,color(stale?"stale":a.status),24);
    text(12,180,a.age<0?"No task observation":"Source age: "+String(a.age)+"s",MUTED,30);
    text(12,210,"Read-only supervised task",MUTED,30);
    text(12,235,"Tap body to return",MUTED,30);
  } else {
    for(int i=0;i<data.count;i++) {
      int x=(i%2)*120,y=52+(i/2)*72; Item &a=data.items[i];
      lcd.drawRect(x+3,y,114,68,0x3186); avatar(x+8,y+6,i,color(stale?"stale":a.status));
      text(x+40,y+8,a.machine,FG,11); text(x+40,y+21,a.session,MUTED,11);
      text(x+8,y+44,stale?"stale":a.status,color(stale?"stale":a.status),17);
    }
    if(!data.count) text(12,100,valid?"No matching tasks":"No live snapshot",MUTED,30);
  }
  lcd.drawFastHLine(0,280,240,MUTED);
  text(5,291,demo?"LIVE":"DEMO",0x07FF,6);
  text(78,291,filtered?"WORK":"ALL",0x07FF,6);
  text(155,291,"NEXT "+String(page+1),0x07FF,12);
  xSemaphoreGive(lock);
}
// Parse a complete bounded response into a temporary snapshot; never partially publish.
bool decode(const String &body,Snapshot &out) {
  StaticJsonDocument<4096> doc;
  if(deserializeJson(doc,body) || doc["version"]!=1 || !doc["items"].is<JsonArray>() ||
     !doc["running"].is<int>() || !doc["total"].is<int>() || !doc["page"].is<int>()) return false;
  JsonArray items=doc["items"];
  if(items.size()>6) return false;
  out.running=doc["running"]; out.total=doc["total"];
  if(out.running<0 || out.total<out.running || out.total>120) return false;
  out.next=doc["next_page"].is<int>()?doc["next_page"].as<int>():-1;
  if(out.next>1000 || out.next< -1) return false;
  for(JsonObject a:items) {
    for(const char *key:{"id","machine","session","status"}) {
      if(!a[key].is<const char*>()) return false;
      const char *s=a[key]; if(strlen(s)>24 || !strlen(s)) return false;
      for(;*s;s++) if(*s<32 || *s>126) return false;
    }
    String s=a["status"].as<String>();
    if(s!="working" && s!="pending" && s!="waiting" && s!="done" && s!="blocked" &&
       s!="failed" && s!="cancelled" && s!="stale" && s!="unknown") return false;
    Item &i=out.items[out.count++]; i.id=a["id"].as<String>();
    i.machine=a["machine"].as<String>(); i.session=a["session"].as<String>(); i.status=s;
    i.age=a["age_s"].is<int>()?a["age_s"].as<int>():-1;
  }
  return true;
}
void fetchTask(void*) {
  for(;;) {
    ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
    xSemaphoreTake(lock,portMAX_DELAY);
    uint32_t requestGeneration=generation; int requestPage=page; bool requestFilter=filtered;
    xSemaphoreGive(lock);
    Snapshot result; String state="OFFLINE WiFi"; bool ok=false;
    if(WiFi.status()==WL_CONNECTED) {
      if(time(nullptr)<1735689600) state="Waiting for TLS clock";
      else {
        WiFiClientSecure tls; tls.setCACert(DECK_ROOT_CA); tls.setHandshakeTimeout(5);
        HTTPClient http; http.setConnectTimeout(4000); http.setTimeout(4000);
        http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
        String url=String(DECK_API_URL)+"?page="+String(requestPage)+"&filter="+(requestFilter?"working":"all");
        if(url.startsWith("https://") && http.begin(tls,url)) {
          http.addHeader("Authorization",String("Bearer ")+DECK_DEVICE_TOKEN);
          int code=http.GET(); state=code==401?"AUTH denied":code==429?"RATE limited":"TLS/API error";
          // Require Content-Length, cap bytes before reading; no chunked/unbounded body.
          int size=http.getSize();
          if(code==200 && size>0 && size<=4096) {
            String body; body.reserve(size);
            WiFiClient *stream=http.getStreamPtr(); uint32_t started=millis();
            while(body.length()<unsigned(size) && millis()-started<5000) {
              while(stream->available() && body.length()<unsigned(size)) body+=char(stream->read());
              vTaskDelay(pdMS_TO_TICKS(5));
            }
            ok=body.length()==unsigned(size) && decode(body,result);
            if(ok) state="LIVE WiFi "+String(WiFi.RSSI())+"dBm";
            else state="Invalid API response";
          }
          http.end();
        }
      }
    }
    xSemaphoreTake(lock,portMAX_DELAY);
    if(!demo && generation==requestGeneration) {
      network=state;
      if(ok) { data=result; lastGood=millis(); valid=true; }
    }
    busy=false;
    xSemaphoreGive(lock);
  }
}
bool touch(int &x,int &y) {
  Wire.beginTransmission(0x38); Wire.write(uint8_t(2));
  if(Wire.endTransmission(false)!=0 || Wire.requestFrom(0x38,5)!=5) return false;
  uint8_t b[5]; for(auto &v:b) v=Wire.read();
  if((b[0]&15)!=1 || (b[1]>>6)==1) return false;
  x=((b[1]&15)<<8)|b[2]; y=((b[3]&15)<<8)|b[4];
  if(DECK_TOUCH_SWAP_XY) { int t=x; x=y; y=t; }
  if(DECK_TOUCH_FLIP_X) x=239-x;
  if(DECK_TOUCH_FLIP_Y) y=319-y;
  return x>=0 && x<240 && y>=0 && y<320;
}
void setup() {
  pinMode(1,OUTPUT); digitalWrite(1,LOW); // candidate board speaker amplifier off
  SPI.begin(LCD_SCK,LCD_MISO,LCD_MOSI,LCD_CS);
  lcd.begin(40000000); lcd.setRotation(0);
  ledcSetup(0,5000,8); ledcAttachPin(LCD_BL,0); ledcWrite(0,180);
  Wire.begin(TOUCH_SDA,TOUCH_SCL); Wire.setTimeOut(30);
  pinMode(TOUCH_INT,INPUT); pinMode(TOUCH_RST,OUTPUT);
  digitalWrite(TOUCH_RST,LOW); delay(10); digitalWrite(TOUCH_RST,HIGH); delay(200);
  lock=xSemaphoreCreateMutex();
  xTaskCreate(fetchTask,"deck-https",16384,nullptr,1,&worker);
  if(demo) makeDemo();
  lastTouch=millis(); lastAttempt=millis()-20000; draw();
}
void loop() {
  static bool held=false, wasDemo=true; static uint32_t redraw=0, reconnect=0;
  int x,y; bool down=touch(x,y); uint32_t now=millis();
  if(down && !held) {
    bool wake=now-lastTouch>30000; lastTouch=now;
    if(!wake) {
      xSemaphoreTake(lock,portMAX_DELAY);
      if(y>=280) {
        generation++; selectedCard=-1;
        if(x<70) { demo=!demo; page=0; data=Snapshot(); valid=false; }
        else if(x<145) { filtered=!filtered; page=0; data=Snapshot(); valid=false; }
        else if(data.next>=0) { page=data.next; data=Snapshot(); valid=false; }
        else { page=0; data=Snapshot(); valid=false; }
        if(demo) makeDemo();
      } else if(selectedCard>=0) selectedCard=-1;
      else if(y>=52 && y<268) { int i=((y-52)/72)*2+x/120; if(i<data.count) selectedCard=i; }
      xSemaphoreGive(lock);
    }
    draw();
  }
  held=down; ledcWrite(0,now-lastTouch>30000?18:180);
  if(demo!=wasDemo) {
    if(demo) { WiFi.disconnect(true); WiFi.mode(WIFI_OFF); }
    else reconnect=now-30000;
    wasDemo=demo;
  }
  if(!demo && strlen(DECK_WIFI_SSID) && strlen(DECK_DEVICE_TOKEN)>=32 && strlen(DECK_ROOT_CA)>0) {
    if(WiFi.status()!=WL_CONNECTED && now-reconnect>=30000) {
      reconnect=now; WiFi.mode(WIFI_STA); WiFi.setSleep(true);
      WiFi.begin(DECK_WIFI_SSID,DECK_WIFI_PASSWORD); configTime(0,0,"pool.ntp.org","time.cloudflare.com");
    }
    xSemaphoreTake(lock,portMAX_DELAY);
    if(!busy && now-lastAttempt>=20000) { busy=true; lastAttempt=now; xTaskNotifyGive(worker); }
    xSemaphoreGive(lock);
  } else if(!demo) {
    xSemaphoreTake(lock,portMAX_DELAY); network="LIVE needs local config"; xSemaphoreGive(lock);
  }
  if(now-redraw>=5000) { redraw=now; draw(); }
  delay(20);
}
