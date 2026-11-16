/* Quella ESP8266 network gateway.
 * UART0 is exclusively the 115200-baud binary QG link to Mega Serial3.
 * Raw frames leave only through binary UDP; WebSocket remains status/control.
 * The ESP contains no appliance safety decisions. Mutating HTTP calls complete
 * only after a Mega result.
 */
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <WebSocketsServer.h>
#include <WiFiUdp.h>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy secrets.example.h to secrets.h before compiling"
#endif
#ifndef QUELLA_WIFI_SERIAL_DEBUG
#define QUELLA_WIFI_SERIAL_DEBUG 0
#endif
#ifndef QUELLA_WIFI_USE_STATIC_IP
#define QUELLA_WIFI_USE_STATIC_IP 0
#endif
#if !QUELLA_WIFI_SERIAL_DEBUG
static_assert(sizeof(QUELLA_API_KEY)>16,"QUELLA_API_KEY must be at least 16 characters");
#endif
#ifndef QUELLA_SERIAL1_LINK_DEBUG
#define QUELLA_SERIAL1_LINK_DEBUG 0
#endif
#ifndef QUELLA_TELEMETRY_HOST
#define QUELLA_TELEMETRY_HOST "0.0.0.0"
#endif
#ifndef QUELLA_TELEMETRY_PORT
#define QUELLA_TELEMETRY_PORT 42042
#endif

static const uint32_t MEGA_BAUD=115200UL;
static const uint8_t MAGIC0='Q',MAGIC1='G',VERSION=1,MAX_PAYLOAD=192;
static const uint32_t MEGA_LINK_TIMEOUT_MS=1000UL,COMMAND_TIMEOUT_MS=2000UL;
enum MessageType:uint8_t {
 MSG_PING=0x01,MSG_GET_STATUS=0x02,MSG_DISPENSE=0x03,MSG_STOP_DISPENSE=0x04,
 MSG_SET_PRESET=0x05,MSG_SET_TANK_DELAY=0x06,MSG_GET_CONFIG=0x07,
 MSG_GET_OVERRIDES=0x08,MSG_SET_OVERRIDE=0x09,MSG_DELETE_OVERRIDE=0x0A,MSG_SAVE_CONFIG=0x0B,
 MSG_HELLO=0x80,MSG_PONG=0x81,MSG_STATUS_SNAPSHOT=0x82,MSG_EVENT=0x83,
 MSG_COMMAND_RESULT=0x84,MSG_CONFIG_RESULT=0x85,MSG_ERROR=0x86,MSG_RAW_BATCH=0x87,MSG_OVERRIDES_RESULT=0x88
};
enum ResultCode:uint8_t {
 RESULT_OK=0,RESULT_BUSY=1,RESULT_TANK_EMPTY=2,RESULT_EXTERNAL_NOT_READY=3,
 RESULT_TEMP_OUT_OF_RANGE=4,RESULT_INVALID_VOLUME=5,RESULT_COMMUNICATION_ERROR=6,
 RESULT_FILL_ACTIVE=7,RESULT_FLUSH_ACTIVE=8,RESULT_INVALID_REQUEST=9,
 RESULT_NOT_ACTIVE=10,RESULT_QUEUE_FULL=11,RESULT_UNSUPPORTED=12,
 RESULT_INTERNAL_TANK_BELOW_LOW=13
};
enum KnownBit:uint8_t {
 K_A5_D11_BIT4_CYCLIC=0,K_DISPENSING=1,K_FILLING=2,K_FLUSHING=3,K_HEATING=4,K_PUMP=5,K_VALVE=6,K_ERROR=7,
 K_TARGET=8,K_CURRENT=9,K_REMAINING=10,K_FLOW=11,K_PROGRESS=12,K_REQUESTED_TEMP=13,
 K_INLET_TEMP=14,K_OUTLET_TEMP=15,K_INTERNAL_MIN=16,K_INTERNAL_MAX=17,K_EXTERNAL_LOW=18,
 K_EXTERNAL_RAW=19,K_EXTERNAL_STABLE=20,K_FILL_ALLOWED=21,K_FILL_ACTIVE=22,K_AC_MAINS=23,
 K_TOTAL_DISPENSED=24,K_CURRENT_COUNTER=25,K_FEED_PARAMETER=26,K_MODE=27,K_ERROR_CODES=28,K_TDS=29
};

ESP8266WebServer http(80);
WebSocketsServer websocket(81);
WiFiUDP telemetryUdp;
IPAddress telemetryHost;
bool telemetryConfigured=false;
uint16_t nextSequence=1;uint32_t lastMegaMs=0,lastWifiAttemptMs=0;
struct StatusCache {bool valid=false;uint8_t raw[46];} statusCache;
struct ConfigCache {bool valid=false;uint8_t raw[49];} configCache;
struct OverridesCache {bool valid=false;uint8_t raw[58];} overridesCache;
struct PendingResult {bool waiting=false,received=false;uint16_t sequence=0;uint8_t command=0,result=RESULT_COMMUNICATION_ERROR;} pending;
uint32_t udpSequence=0,udpPacketsSent=0,udpSendErrors=0,rawFramesReceived=0;
uint32_t rawFramesSent=0,rawFramesDropped=0,qgRawBatchesReceived=0,megaRawFramesDropped=0;
uint32_t lastQGRawBatchMs=0,lastUdpSendMs=0,lastWsStatusMs=0;
uint8_t lastRawBatchFrames=0;

static uint16_t get16(const uint8_t*p){return p[0]|((uint16_t)p[1]<<8);}
static uint32_t get32(const uint8_t*p){return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void put16(uint8_t*p,uint16_t v){p[0]=v;p[1]=v>>8;}
static void put32(uint8_t*p,uint32_t v){p[0]=v;p[1]=v>>8;p[2]=v>>16;p[3]=v>>24;}
static uint16_t crc16(const uint8_t*p,uint16_t n){uint16_t c=0xFFFF;while(n--){c^=(uint16_t)*p++<<8;for(uint8_t b=0;b<8;b++)c=c&0x8000?(c<<1)^0x1021:c<<1;}return c;}
static uint16_t crcContinue(uint16_t c,const uint8_t*p,uint16_t n){while(n--){c^=(uint16_t)*p++<<8;for(uint8_t b=0;b<8;b++)c=c&0x8000?(c<<1)^0x1021:c<<1;}return c;}
bool sendPacket(uint8_t type,uint16_t sequence,const uint8_t*payload,uint16_t length){
 if(length>MAX_PAYLOAD)return false;uint8_t header[9]={MAGIC0,MAGIC1,VERSION,type,0,0,0,0,0};
 put16(header+5,sequence);put16(header+7,length);uint16_t crc=crcContinue(crc16(header+2,7),payload,length);
 Serial.write(header,sizeof(header));if(length)Serial.write(payload,length);uint8_t tail[2];put16(tail,crc);Serial.write(tail,2);return true;
}
uint16_t allocateSequence(){if(++nextSequence==0)nextSequence=1;return nextSequence;}
bool megaLinked(){return lastMegaMs&&(uint32_t)(millis()-lastMegaMs)<=MEGA_LINK_TIMEOUT_MS;}
const char* resultName(uint8_t result){switch(result){case RESULT_OK:return "OK";case RESULT_BUSY:return "BUSY";case RESULT_TANK_EMPTY:return "TANK_EMPTY";
 case RESULT_EXTERNAL_NOT_READY:return "EXTERNAL_TANK_NOT_READY";case RESULT_TEMP_OUT_OF_RANGE:return "TEMP_OUT_OF_RANGE";
 case RESULT_INVALID_VOLUME:return "INVALID_VOLUME";case RESULT_COMMUNICATION_ERROR:return "COMMUNICATION_ERROR";
 case RESULT_FILL_ACTIVE:return "FILL_ACTIVE";case RESULT_FLUSH_ACTIVE:return "FLUSH_ACTIVE";case RESULT_INVALID_REQUEST:return "INVALID_REQUEST";
 case RESULT_NOT_ACTIVE:return "NOT_ACTIVE";case RESULT_QUEUE_FULL:return "QUEUE_FULL";
 case RESULT_INTERNAL_TANK_BELOW_LOW:return "INTERNAL_TANK_BELOW_LOW";default:return "UNSUPPORTED";}}
const char* modeName(uint8_t mode){static const char*names[]={"UNKNOWN","IDLE","IDLE","DISPENSE","FILL","FLUSH","HEATING","ERROR"};return mode<8?names[mode]:"UNKNOWN";}
const char* originName(uint8_t origin){return origin==1?"DISPLAY_OVERRIDE":(origin==2?"MANUAL_REMOTE":"NONE");}
const char* frameKindName(uint8_t kind){switch(kind){
 case 1:return "A5_REQUEST";case 2:return "E5_B2_REQUEST";case 3:return "E5_E1_REQUEST";
 case 4:return "AA00";case 5:return "AA_A3";case 6:return "AA_E1";case 7:return "AA_F1";
 case 8:return "A5_RESPONSE";case 9:return "E5_B2_RESPONSE";case 10:return "E5_E1_RESPONSE";
 case 11:return "CC";case 12:return "AA_OTHER";default:return "UNKNOWN";}}
void appendBool(String&json,const char*name,bool known,bool value,bool comma=true){json+='\"';json+=name;json+="\":";json+=known?(value?"true":"false"):"null";if(comma)json+=',';}
void appendNumber(String&json,const char*name,bool known,uint32_t value,bool comma=true){json+='\"';json+=name;json+="\":";if(known)json+=String(value);else json+="null";if(comma)json+=',';}
void appendHexByte(String&json,uint8_t value){const char digits[]="0123456789ABCDEF";json+=digits[value>>4];json+=digits[value&0x0F];}
uint8_t tdsDisplayValue(uint8_t raw){
 static const uint8_t upper[]={0x1F,0x2A,0x36,0x41,0x4C,0x58,0x6E,0x79,0x84,0x8F,0x9A,0xA4,0xAF,0xC4,0xCF,0xDA,0xE4,0xEF,0xF9,0xFF};
 for(uint8_t value=0;value<sizeof(upper);value++)if(raw<=upper[value])return value;
 return 19;
}

String statusJson(){
 if(!statusCache.valid)return "{\"available\":false}";const uint8_t*p=statusCache.raw;uint32_t known=get32(p+5);
 uint8_t flags=p[10],tank=p[28],fill=p[29],runtime=p[12];String json;json.reserve(1800);
 json="{\"available\":true,\"snapshot_version\":"+String(p[0])+",\"mega_uptime_ms\":"+String(get32(p+1));
 json+=",\"machine\":{";json+="\"mode\":\""+String(modeName(p[9]))+"\",";
 appendBool(json,"a5_d11_bit4_cyclic",known&(1UL<<K_A5_D11_BIT4_CYCLIC),flags&1);appendBool(json,"dispensing",known&(1UL<<K_DISPENSING),flags&2);
 appendBool(json,"feed_pump",known&(1UL<<K_DISPENSING),flags&2);
 appendBool(json,"filling",known&(1UL<<K_FILLING),flags&4);appendBool(json,"flushing",known&(1UL<<K_FLUSHING),flags&8);
 appendBool(json,"heating",known&(1UL<<K_HEATING),flags&16);appendBool(json,"pump",known&(1UL<<K_PUMP),flags&32);
 appendBool(json,"valve",known&(1UL<<K_VALVE),flags&64);appendBool(json,"error",known&(1UL<<K_ERROR),flags&128);
 appendNumber(json,"error_code_raw",known&(1UL<<K_ERROR_CODES),get16(p+13),false);json+="},\"dispense\":{";
 appendNumber(json,"target_ml",known&(1UL<<K_TARGET),get16(p+15));appendNumber(json,"current_ml",known&(1UL<<K_CURRENT),get16(p+17));
 appendNumber(json,"remaining_ml",known&(1UL<<K_REMAINING),get16(p+19));json+="\"flow_ml_s\":";
 if(known&(1UL<<K_FLOW))json+=String(get16(p+21)/100.0f,2);else json+="null";json+=",\"progress_percent\":";
 if(known&(1UL<<K_PROGRESS))json+=String(get16(p+23)/10.0f,1);else json+="null";json+=',';
 appendNumber(json,"requested_temp_c",known&(1UL<<K_REQUESTED_TEMP),p[25]);appendNumber(json,"inlet_temp_c",known&(1UL<<K_INLET_TEMP),p[26]);
 appendNumber(json,"outlet_temp_c",known&(1UL<<K_OUTLET_TEMP),p[27],false);json+="},\"tanks\":{";
 appendBool(json,"internal_min",known&(1UL<<K_INTERNAL_MIN),tank&1);appendBool(json,"internal_max",known&(1UL<<K_INTERNAL_MAX),tank&2);
 appendBool(json,"external_low",known&(1UL<<K_EXTERNAL_LOW),tank&4);appendBool(json,"external_raw_available",known&(1UL<<K_EXTERNAL_RAW),tank&8);
 appendBool(json,"external_stable_available",known&(1UL<<K_EXTERNAL_STABLE),tank&16);appendBool(json,"fill_allowed",known&(1UL<<K_FILL_ALLOWED),fill&1);
 appendBool(json,"fill_active",known&(1UL<<K_FILL_ACTIVE),fill&2);
 appendBool(json,"external_empty_stop_latched",true,fill&8);appendBool(json,"external_empty_reset_armed",true,fill&16);
 appendNumber(json,"fill_delay_remaining_ms",true,get32(p+30),false);
 json+="},\"water\":{";appendNumber(json,"tds_raw",known&(1UL<<K_TDS),p[45]);
 json+="\"tds_raw_hex\":";if(known&(1UL<<K_TDS)){json+="\"0x";appendHexByte(json,p[45]);json+="\"";}else json+="null";json+=',';
 appendNumber(json,"tds_calculated",known&(1UL<<K_TDS),tdsDisplayValue(p[45]));
 json+="\"tds_mapping\":\"CAPTURED_DISPLAY_THRESHOLDS_V1\",";appendNumber(json,"tds",known&(1UL<<K_TDS),p[45],false);json+="},\"electrical\":{";
 appendNumber(json,"ac_mains_v",known&(1UL<<K_AC_MAINS),p[34],false);json+="},\"statistics\":{";
 appendNumber(json,"total_dispensed_ml",known&(1UL<<K_TOTAL_DISPENSED),get32(p+35));appendNumber(json,"current_counter",known&(1UL<<K_CURRENT_COUNTER),get16(p+39));
 appendNumber(json,"feed_pump_parameter",known&(1UL<<K_FEED_PARAMETER),get16(p+41),false);json+="},\"runtime\":{";
 json+="\"session_origin\":\""+String(originName(p[11]))+"\",";appendBool(json,"override_active",true,runtime&1);appendBool(json,"remote_active",true,runtime&2);
 appendBool(json,"aborting",true,runtime&4,false);json+="}}";return json;
}
String configJson(){
 if(!configCache.valid)return "{\"available\":false}";const uint8_t*p=configCache.raw;String json;json.reserve(700);
 json="{\"available\":true,\"version\":"+String(p[0])+",\"override_api\":"+String(p[0]>=2?"true":"false")+",\"tank_delay\":{\"enabled\":"+String(p[1]?"true":"false")+
  ",\"delay_ms\":"+String(get32(p+2))+"},\"max_remote_volume_ml\":"+String(get16(p+6))+",\"presets\":[";
 uint8_t count=min((uint8_t)8,p[8]);for(uint8_t i=0;i<count;i++){if(i)json+=',';const uint8_t*e=p+9+i*5;
  json+="{\"id\":"+String(e[0])+",\"configured\":"+String(e[1]?"true":"false")+",\"ml\":"+String(get16(e+2))+",\"temperature_c\":"+String(e[4])+"}";}
 json+="]}";return json;
}
String overridesJson(){
 if(!overridesCache.valid)return "{\"available\":false,\"rules\":[]}";const uint8_t*p=overridesCache.raw;
 String json="{\"available\":true,\"version\":"+String(p[0])+",\"rules\":[";bool first=true;
 uint8_t count=min((uint8_t)8,p[1]);for(uint8_t i=0;i<count;i++){const uint8_t*e=p+2+i*7;if(!e[1])continue;
  if(!first)json+=',';first=false;json+="{\"id\":"+String(e[0])+",\"type\":\"VOLUME_PRESET\",\"source_value\":"+String(get16(e+3))+
   ",\"target_value\":"+String(get16(e+5))+",\"enabled\":"+String(e[2]?"true":"false")+"}";}
 json+="]}";return json;
}
void broadcast(const String&type,const String&data){
 if(websocket.connectedClients()==0)return;
 String message;message.reserve(type.length()+data.length()+22);
 message="{\"type\":\"";message+=type;message+="\",\"data\":";message+=data;message+='}';
 websocket.broadcastTXT(message);
}

void sendRawTelemetry(const uint8_t*p,uint16_t length){
 qgRawBatchesReceived++;lastQGRawBatchMs=millis();
 if(length<8||p[0]!=1){rawFramesDropped++;return;}
 uint8_t frameCount=p[3];uint16_t megaBatch=get16(p+1);megaRawFramesDropped=get32(p+4);
 uint16_t offset=8;
 for(uint8_t i=0;i<frameCount;i++){
  if(offset+7>length){rawFramesDropped+=frameCount-i;return;}
  uint8_t rawLength=p[offset+6];
  if(rawLength>19||offset+7+rawLength>length){rawFramesDropped+=frameCount-i;return;}
  offset+=7+rawLength;
 }
 if(offset!=length){rawFramesDropped++;return;}
 rawFramesReceived+=frameCount;lastRawBatchFrames=frameCount;
 if(!telemetryConfigured||WiFi.status()!=WL_CONNECTED){rawFramesDropped+=frameCount;return;}
 uint8_t datagram[224];uint16_t payloadLength=length-8,total=22+payloadLength+2;
 if(total>sizeof(datagram)){rawFramesDropped+=frameCount;return;}
 datagram[0]='Q';datagram[1]='T';datagram[2]=2;datagram[3]=1;
 uint32_t sequence=++udpSequence;put32(datagram+4,sequence);put32(datagram+8,millis());
 datagram[12]=frameCount;put16(datagram+13,payloadLength);put16(datagram+15,megaBatch);put32(datagram+17,megaRawFramesDropped);
 datagram[21]=(uint8_t)(int8_t)WiFi.RSSI();
 memcpy(datagram+22,p+8,payloadLength);
 uint16_t crc=crc16(datagram+2,20+payloadLength);put16(datagram+22+payloadLength,crc);
 bool began=telemetryUdp.beginPacket(telemetryHost,QUELLA_TELEMETRY_PORT)==1;
 size_t written=began?telemetryUdp.write(datagram,total):0;
 bool sent=began&&written==total&&telemetryUdp.endPacket()==1;
 if(sent){udpPacketsSent++;rawFramesSent+=frameCount;lastUdpSendMs=millis();}
 else{udpSendErrors++;rawFramesDropped+=frameCount;}
}

struct Parser {uint8_t state=0,h[7],hp=0,p[MAX_PAYLOAD],crcBytes[2],crcPos=0;uint16_t len=0,pos=0;} parser;
void resetParser(){parser=Parser();}
void handlePacket(uint8_t type,uint16_t sequence,const uint8_t*payload,uint16_t length){
 lastMegaMs=millis();
 if(type==MSG_STATUS_SNAPSHOT&&length==46){
  // Bytes 1..4 are Mega uptime and change on every 5 Hz snapshot. They are
  // transport metadata, not a semantic machine-state change; comparing them
  // caused a full JSON broadcast every 200 ms and could overwhelm WS clients.
  bool changed=!statusCache.valid||memcmp(statusCache.raw+5,payload+5,41)!=0;
  memcpy(statusCache.raw,payload,46);statusCache.valid=true;if(changed){String json=statusJson();broadcast("status",json);lastWsStatusMs=millis();
   if(QUELLA_SERIAL1_LINK_DEBUG)Serial1.println(json);}return;}
 if(type==MSG_CONFIG_RESULT&&length==49){memcpy(configCache.raw,payload,49);configCache.valid=true;
  if(pending.waiting&&pending.sequence==sequence){pending.received=true;pending.result=RESULT_OK;}return;}
 if(type==MSG_OVERRIDES_RESULT&&length==58){memcpy(overridesCache.raw,payload,58);overridesCache.valid=true;
  if(pending.waiting&&pending.sequence==sequence){pending.received=true;pending.result=RESULT_OK;}return;}
 if(type==MSG_COMMAND_RESULT&&length==2){String data="{\"command\":"+String(payload[0])+",\"result\":\""+String(resultName(payload[1]))+"\"}";
  broadcast("command_result",data);if(pending.waiting&&pending.sequence==sequence){pending.command=payload[0];pending.result=payload[1];pending.received=true;}return;}
 if(type==MSG_EVENT&&length>=6){String data="{\"domain\":"+String(payload[0])+",\"event\":"+String(payload[1])+",\"mega_uptime_ms\":"+String(get32(payload+2))+"}";broadcast("event",data);return;}
 if(type==MSG_RAW_BATCH){sendRawTelemetry(payload,length);return;}
 if(type==MSG_ERROR){String data="{\"code\":"+String(length?payload[0]:255)+"}";broadcast("error",data);
  if(pending.waiting&&pending.sequence==sequence){pending.result=RESULT_COMMUNICATION_ERROR;pending.received=true;}return;}
}
void serviceMega(){
 while(Serial.available()){uint8_t v=(uint8_t)Serial.read();if(parser.state==0){if(v==MAGIC0)parser.state=1;}
  else if(parser.state==1){if(v==MAGIC1){parser.state=2;parser.hp=0;}else parser.state=v==MAGIC0?1:0;}
  else if(parser.state==2){parser.h[parser.hp++]=v;if(parser.hp==7){parser.len=get16(parser.h+5);
    if(parser.h[0]!=VERSION||parser.len>MAX_PAYLOAD)resetParser();else{parser.pos=parser.crcPos=0;parser.state=parser.len?3:4;}}}
  else if(parser.state==3){parser.p[parser.pos++]=v;if(parser.pos==parser.len)parser.state=4;}
  else{parser.crcBytes[parser.crcPos++]=v;if(parser.crcPos==2){uint16_t c=crcContinue(crc16(parser.h,7),parser.p,parser.len);
    if(c==get16(parser.crcBytes))handlePacket(parser.h[1],get16(parser.h+3),parser.p,parser.len);resetParser();}}}
}

bool authorized(){return http.hasHeader("X-API-Key")&&http.header("X-API-Key")==QUELLA_API_KEY;}
void sendJson(int code,const String&body){http.sendHeader("Cache-Control","no-store");http.send(code,"application/json",body);}
bool readInteger(const String&body,const char*key,long&value){
 String needle="\""+String(key)+"\"";int at=body.indexOf(needle);if(at<0)return false;at=body.indexOf(':',at+needle.length());if(at<0)return false;
 at++;while(at<(int)body.length()&&isSpace(body[at]))at++;bool negative=at<(int)body.length()&&body[at]=='-';if(negative)at++;
 if(at>=(int)body.length()||!isDigit(body[at]))return false;long parsed=0;while(at<(int)body.length()&&isDigit(body[at]))parsed=parsed*10+(body[at++]-'0');
 value=negative?-parsed:parsed;return true;
}
bool readBool(const String&body,const char*key,bool&value){
 String needle="\""+String(key)+"\"";int at=body.indexOf(needle);if(at<0)return false;at=body.indexOf(':',at+needle.length());if(at<0)return false;
 String tail=body.substring(at+1);tail.trim();if(tail.startsWith("true")){value=true;return true;}if(tail.startsWith("false")){value=false;return true;}return false;
}
bool transact(uint8_t command,const uint8_t*payload,uint16_t length,uint8_t&result){
 if(!megaLinked())return false;pending=PendingResult();pending.waiting=true;pending.sequence=allocateSequence();pending.command=command;
 sendPacket(command,pending.sequence,payload,length);uint32_t started=millis();
 while(!pending.received&&(uint32_t)(millis()-started)<COMMAND_TIMEOUT_MS){serviceMega();websocket.loop();yield();}
 bool received=pending.received;result=pending.result;pending.waiting=false;return received;
}
int resultHttpCode(uint8_t result){if(result==RESULT_OK)return 200;if(result==RESULT_COMMUNICATION_ERROR)return 503;
 if(result==RESULT_INVALID_REQUEST||result==RESULT_INVALID_VOLUME||result==RESULT_TEMP_OUT_OF_RANGE)return 422;return 409;}
void commandReply(bool received,uint8_t result){
 if(!received){sendJson(504,"{\"ok\":false,\"result\":\"GATEWAY_TIMEOUT\"}");return;}
 sendJson(resultHttpCode(result),"{\"ok\":"+String(result==RESULT_OK?"true":"false")+",\"result\":\""+String(resultName(result))+"\"}");
}
bool requireMutationAccess(){
 if(!authorized()){sendJson(401,"{\"error\":\"UNAUTHORIZED\"}");return false;}
 if(!megaLinked()){sendJson(503,"{\"error\":\"MEGA_LINK_DOWN\"}");return false;}return true;
}
void setupHttp(){
 http.collectHeaders("X-API-Key");
 http.on("/api/status",HTTP_GET,[]{if(!megaLinked()){sendJson(503,"{\"available\":false,\"mega_link\":false}");return;}sendJson(200,statusJson());});
 http.on("/api/health",HTTP_GET,[]{String body="{\"wifi\":"+String(WiFi.status()==WL_CONNECTED?"true":"false")+
  ",\"mega_link\":"+String(megaLinked()?"true":"false")+",\"mega_last_seen_ms\":"+(lastMegaMs?String(millis()-lastMegaMs):"null")+
  ",\"uptime_ms\":"+String(millis())+",\"free_heap\":"+String(ESP.getFreeHeap())+
  ",\"telemetry_configured\":"+String(telemetryConfigured?"true":"false")+
  ",\"udp_packets_sent\":"+String(udpPacketsSent)+",\"udp_send_errors\":"+String(udpSendErrors)+
  ",\"raw_frames_received\":"+String(rawFramesReceived)+",\"raw_frames_sent\":"+String(rawFramesSent)+
  ",\"raw_frames_dropped\":"+String(rawFramesDropped)+",\"mega_raw_frames_dropped\":"+String(megaRawFramesDropped)+
  ",\"qg_raw_batches_received\":"+String(qgRawBatchesReceived)+
  ",\"last_qg_raw_batch_age_ms\":"+(lastQGRawBatchMs?String(millis()-lastQGRawBatchMs):"null")+
  ",\"last_udp_send_age_ms\":"+(lastUdpSendMs?String(millis()-lastUdpSendMs):"null")+
  ",\"last_raw_batch_frames\":"+String(lastRawBatchFrames)+"}";sendJson(megaLinked()?200:503,body);});
 http.on("/api/config",HTTP_GET,[]{if(!megaLinked()){sendJson(503,"{\"error\":\"MEGA_LINK_DOWN\"}");return;}uint8_t result;
  if(!transact(MSG_GET_CONFIG,nullptr,0,result)){sendJson(504,"{\"error\":\"GATEWAY_TIMEOUT\"}");return;}sendJson(200,configJson());});
 http.on("/api/overrides",HTTP_GET,[]{if(!megaLinked()){sendJson(503,"{\"error\":\"MEGA_LINK_DOWN\"}");return;}uint8_t result;
  if(!transact(MSG_GET_OVERRIDES,nullptr,0,result)){sendJson(504,"{\"error\":\"GATEWAY_TIMEOUT\"}");return;}sendJson(200,overridesJson());});
 http.on("/api/dispense",HTTP_POST,[]{if(!requireMutationAccess())return;long ml,temp;String body=http.arg("plain");
  if(!readInteger(body,"ml",ml)||!readInteger(body,"temperature_c",temp)||ml<1||ml>1500||temp<0||temp>100){sendJson(400,"{\"error\":\"INVALID_JSON_OR_RANGE\"}");return;}
  uint8_t p[3],result;put16(p,(uint16_t)ml);p[2]=(uint8_t)temp;commandReply(transact(MSG_DISPENSE,p,3,result),result);});
 http.on("/api/dispense/stop",HTTP_POST,[]{if(!requireMutationAccess())return;uint8_t result;commandReply(transact(MSG_STOP_DISPENSE,nullptr,0,result),result);});
 http.on("/api/tank-delay",HTTP_POST,[]{if(!requireMutationAccess())return;bool enabled;long delay;String body=http.arg("plain");
  if(!readBool(body,"enabled",enabled)||!readInteger(body,"delay_ms",delay)||delay<0||delay>60000){sendJson(400,"{\"error\":\"INVALID_JSON_OR_RANGE\"}");return;}
  uint8_t p[5],result;p[0]=enabled;put32(p+1,(uint32_t)delay);bool received=transact(MSG_SET_TANK_DELAY,p,5,result);
  if(received&&result==RESULT_OK){uint8_t saved;transact(MSG_SAVE_CONFIG,nullptr,0,saved);}commandReply(received,result);});
 http.on("/api/preset",HTTP_POST,[]{if(!requireMutationAccess())return;long id,ml,temp=25;String body=http.arg("plain");
  if(!readInteger(body,"id",id)||!readInteger(body,"ml",ml)||id<0||id>7||ml<1||ml>1500){sendJson(400,"{\"error\":\"INVALID_JSON_OR_RANGE\"}");return;}
  bool hasTemp=readInteger(body,"temperature_c",temp);if(hasTemp&&(temp<0||temp>100)){sendJson(400,"{\"error\":\"INVALID_JSON_OR_RANGE\"}");return;}
  uint8_t p[4],result;p[0]=(uint8_t)id;put16(p+1,(uint16_t)ml);p[3]=(uint8_t)temp;bool received=transact(MSG_SET_PRESET,p,hasTemp?4:3,result);
  if(received&&result==RESULT_OK){uint8_t saved;transact(MSG_SAVE_CONFIG,nullptr,0,saved);}commandReply(received,result);});
 http.on("/api/overrides",HTTP_POST,[]{if(!requireMutationAccess())return;long source,target;bool enabled;String body=http.arg("plain");
  if(!readInteger(body,"source_value",source)||!readInteger(body,"target_value",target)||!readBool(body,"enabled",enabled)||source<1||source>900||target<1||target>1500){sendJson(400,"{\"error\":\"INVALID_JSON_OR_RANGE\"}");return;}
  uint8_t result;if(!transact(MSG_GET_OVERRIDES,nullptr,0,result)||!overridesCache.valid){sendJson(504,"{\"error\":\"GATEWAY_TIMEOUT\"}");return;}
  int8_t id=-1,freeId=-1;for(uint8_t i=0;i<8;i++){const uint8_t*e=overridesCache.raw+2+i*7;if(e[1]&&get16(e+3)==source)id=i;else if(!e[1]&&freeId<0)freeId=i;}
  if(id<0)id=freeId;if(id<0){sendJson(409,"{\"error\":\"OVERRIDE_TABLE_FULL\"}");return;}
  uint8_t p[6];p[0]=(uint8_t)id;put16(p+1,(uint16_t)source);put16(p+3,(uint16_t)target);p[5]=enabled;
  bool received=transact(MSG_SET_OVERRIDE,p,6,result);if(received&&result==RESULT_OK){uint8_t saved;transact(MSG_SAVE_CONFIG,nullptr,0,saved);overridesCache.valid=false;}commandReply(received,result);});
 http.on("/api/overrides/delete",HTTP_POST,[]{if(!requireMutationAccess())return;long id;String body=http.arg("plain");
  if(!readInteger(body,"id",id)||id<0||id>7){sendJson(400,"{\"error\":\"INVALID_JSON_OR_RANGE\"}");return;}
  uint8_t p=(uint8_t)id,result;bool received=transact(MSG_DELETE_OVERRIDE,&p,1,result);
  if(received&&result==RESULT_OK){uint8_t saved;transact(MSG_SAVE_CONFIG,nullptr,0,saved);overridesCache.valid=false;}commandReply(received,result);});
 http.on("/ws",HTTP_GET,[]{http.send(426,"application/json","{\"error\":\"WEBSOCKET_UPGRADE_REQUIRED\",\"url\":\"ws://"+WiFi.localIP().toString()+":81/ws\"}");});
 http.onNotFound([]{sendJson(404,"{\"error\":\"NOT_FOUND\"}");});http.begin();
}
void websocketEvent(uint8_t client,WStype_t type,uint8_t*,size_t){
 if(type==WStype_CONNECTED&&statusCache.valid){String message="{\"type\":\"status\",\"data\":"+statusJson()+"}";websocket.sendTXT(client,message);}
}
void serviceWebsocketStatus(){
 if(websocket.connectedClients()==0||!statusCache.valid||(uint32_t)(millis()-lastWsStatusMs)<1000UL)return;
 lastWsStatusMs=millis();broadcast("status",statusJson());
}
bool configureWifiNetwork(){
#if QUELLA_WIFI_USE_STATIC_IP
 IPAddress localIp(QUELLA_WIFI_IP_A,QUELLA_WIFI_IP_B,QUELLA_WIFI_IP_C,QUELLA_WIFI_IP_D);
 IPAddress gateway(QUELLA_WIFI_GATEWAY_A,QUELLA_WIFI_GATEWAY_B,QUELLA_WIFI_GATEWAY_C,QUELLA_WIFI_GATEWAY_D);
 IPAddress subnet(QUELLA_WIFI_SUBNET_A,QUELLA_WIFI_SUBNET_B,QUELLA_WIFI_SUBNET_C,QUELLA_WIFI_SUBNET_D);
 IPAddress dns(QUELLA_WIFI_DNS_A,QUELLA_WIFI_DNS_B,QUELLA_WIFI_DNS_C,QUELLA_WIFI_DNS_D);
 return WiFi.config(localIp,gateway,subnet,dns);
#else
 return true;
#endif
}
void connectWifi(){
 if(WiFi.status()==WL_CONNECTED)return;if((uint32_t)(millis()-lastWifiAttemptMs)<10000UL)return;
 lastWifiAttemptMs=millis();WiFi.mode(WIFI_STA);WiFi.setSleepMode(WIFI_NONE_SLEEP);
 configureWifiNetwork();WiFi.hostname(QUELLA_GATEWAY_HOSTNAME);WiFi.begin(QUELLA_WIFI_SSID,QUELLA_WIFI_PASSWORD);
}
#if QUELLA_WIFI_SERIAL_DEBUG
const char* wifiStatusName(int status){
 switch(status){
  case WL_IDLE_STATUS:return "WL_IDLE_STATUS";
  case WL_NO_SSID_AVAIL:return "WL_NO_SSID_AVAIL";
  case WL_SCAN_COMPLETED:return "WL_SCAN_COMPLETED";
  case WL_CONNECTED:return "WL_CONNECTED";
  case WL_CONNECT_FAILED:return "WL_CONNECT_FAILED";
  case WL_CONNECTION_LOST:return "WL_CONNECTION_LOST";
  case WL_DISCONNECTED:return "WL_DISCONNECTED";
  default:return "UNKNOWN";
 }
}
const char* encryptionName(uint8_t encryption){
 switch(encryption){
  case ENC_TYPE_NONE:return "OPEN";
  case ENC_TYPE_WEP:return "WEP";
  case ENC_TYPE_TKIP:return "WPA/TKIP";
  case ENC_TYPE_CCMP:return "WPA2/CCMP";
  case ENC_TYPE_AUTO:return "AUTO";
  default:return "UNKNOWN";
 }
}
uint32_t lastWifiDebugMs=0,wifiDebugStartedMs=0;
bool wifiScanDone=false,wifiConnectedDetailsPrinted=false;
void printConnectedDetails(){
 Serial.println("CONNECTED");
 Serial.print("IP: ");Serial.println(WiFi.localIP());
 Serial.print("Gateway: ");Serial.println(WiFi.gatewayIP());
 Serial.print("Subnet: ");Serial.println(WiFi.subnetMask());
 Serial.print("RSSI: ");Serial.print(WiFi.RSSI());Serial.println(" dBm");
 Serial.print("Channel: ");Serial.println(WiFi.channel());
 Serial.print("BSSID: ");Serial.println(WiFi.BSSIDstr());
}
void scanWifiNetworks(){
 Serial.println("Scanning WiFi networks...");int count=WiFi.scanNetworks(false,true);
 Serial.print("Networks found: ");Serial.println(count);
 bool configuredFound=false;
 for(int i=0;i<count;i++){
  String ssid=WiFi.SSID(i);bool match=ssid==QUELLA_WIFI_SSID;if(match)configuredFound=true;
  Serial.print("[");Serial.print(i);Serial.print("] SSID: ");Serial.print(ssid);
  Serial.print(" | RSSI: ");Serial.print(WiFi.RSSI(i));Serial.print(" dBm");
  Serial.print(" | Channel: ");Serial.print(WiFi.channel(i));
  Serial.print(" | Encryption: ");Serial.print(encryptionName(WiFi.encryptionType(i)));
  if(match)Serial.print(" | CONFIGURED SSID");Serial.println();
 }
 if(count<0)Serial.println("WiFi scan failed");
 Serial.print("Configured SSID found: ");Serial.println(configuredFound?"YES":"NO");
 WiFi.scanDelete();
}
void setup(){
 Serial.begin(115200);delay(50);Serial.println();Serial.println("ESP8266 boot");
 Serial.print("SSID: ");Serial.println(QUELLA_WIFI_SSID);
 Serial.print("SSID length: ");Serial.println(strlen(QUELLA_WIFI_SSID));
 Serial.println("Connecting...");
 WiFi.persistent(false);WiFi.setAutoReconnect(true);WiFi.mode(WIFI_STA);WiFi.setSleepMode(WIFI_NONE_SLEEP);
 bool networkConfigured=configureWifiNetwork();
#if QUELLA_WIFI_USE_STATIC_IP
 Serial.print("Static IP config: ");Serial.println(networkConfigured?"OK":"FAILED");
 Serial.print("Requested IP: ");Serial.println(IPAddress(QUELLA_WIFI_IP_A,QUELLA_WIFI_IP_B,QUELLA_WIFI_IP_C,QUELLA_WIFI_IP_D));
 Serial.print("Requested Gateway: ");Serial.println(IPAddress(QUELLA_WIFI_GATEWAY_A,QUELLA_WIFI_GATEWAY_B,QUELLA_WIFI_GATEWAY_C,QUELLA_WIFI_GATEWAY_D));
 Serial.print("Requested Subnet: ");Serial.println(IPAddress(QUELLA_WIFI_SUBNET_A,QUELLA_WIFI_SUBNET_B,QUELLA_WIFI_SUBNET_C,QUELLA_WIFI_SUBNET_D));
 Serial.print("Requested DNS: ");Serial.println(IPAddress(QUELLA_WIFI_DNS_A,QUELLA_WIFI_DNS_B,QUELLA_WIFI_DNS_C,QUELLA_WIFI_DNS_D));
#else
 (void)networkConfigured;Serial.println("Network config: DHCP");
#endif
 WiFi.hostname(QUELLA_GATEWAY_HOSTNAME);WiFi.begin(QUELLA_WIFI_SSID,QUELLA_WIFI_PASSWORD);
 wifiDebugStartedMs=millis();lastWifiDebugMs=millis()-500UL;
}
void loop(){
 uint32_t now=millis();int status=WiFi.status();
 if((uint32_t)(now-lastWifiDebugMs)>=500UL){lastWifiDebugMs=now;
  Serial.print("WiFi.status=");Serial.print(status);Serial.print(" (");Serial.print(wifiStatusName(status));Serial.println(")");
 }
 if(status==WL_CONNECTED&&!wifiConnectedDetailsPrinted){wifiConnectedDetailsPrinted=true;printConnectedDetails();}
 if(status!=WL_CONNECTED)wifiConnectedDetailsPrinted=false;
 if(!wifiScanDone&&(uint32_t)(now-wifiDebugStartedMs)>=5000UL){wifiScanDone=true;scanWifiNetworks();}
 yield();
}
#else
void setup(){
 Serial.begin(MEGA_BAUD);if(QUELLA_SERIAL1_LINK_DEBUG)Serial1.begin(115200);WiFi.persistent(false);WiFi.setAutoReconnect(true);
 telemetryConfigured=telemetryHost.fromString(QUELLA_TELEMETRY_HOST)&&telemetryHost!=(uint32_t)0&&QUELLA_TELEMETRY_PORT>0;
 if(telemetryConfigured)telemetryUdp.begin(0);
 lastWifiAttemptMs=millis()-10000UL;connectWifi();
 setupHttp();websocket.begin();websocket.onEvent(websocketEvent);sendPacket(MSG_PING,allocateSequence(),nullptr,0);sendPacket(MSG_GET_STATUS,allocateSequence(),nullptr,0);
}
void loop(){serviceMega();connectWifi();http.handleClient();websocket.loop();serviceWebsocketStatus();yield();}
#endif
