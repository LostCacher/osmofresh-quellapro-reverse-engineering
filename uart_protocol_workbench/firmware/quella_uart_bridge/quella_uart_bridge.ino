/* Quella UART MITM bridge — ATmega2560 side of WeMOS Mega + WiFi R3.
 * Serial=USB/CH340G, Serial1=Display (TX18/RX19), Serial2=Main (TX16/RX17),
 * Serial3=ESP8266 semantic gateway (TX14/RX15).  Serial3 never carries raw
 * Quella frames or debug text.
 */
#include <Arduino.h>
#include <EEPROM.h>

#ifndef QUELLA_GATEWAY_PROTOCOL_STREAM
#define QUELLA_GATEWAY_PROTOCOL_STREAM 1
#endif

static const uint32_t PC_BAUD=115200UL;
static const uint32_t GATEWAY_BAUD=115200UL;
static uint32_t quellaBaud=9600UL, idleGapUs=4000UL;
static bool sniffEnabled=true;
enum BridgeMode:uint8_t { DIRECT_PASS=0, DISPLAY_ONLY_TEST=1 };
enum Direction:uint8_t { DISPLAY_TO_MAIN=0, MAIN_TO_DISPLAY=1 };
enum MessageType:uint8_t {
 MSG_SET_CONFIG=0x01,MSG_START_SNIFF=0x02,MSG_STOP_SNIFF=0x03,
 MSG_INJECT_DISPLAY_TO_MAIN=0x04,MSG_INJECT_MAIN_TO_DISPLAY=0x05,
 MSG_SET_BRIDGE_MODE=0x06,MSG_PING=0x07,MSG_SET_RULE=0x08,MSG_CLEAR_RULE=0x09,
 MSG_ENABLE_RULE=0x0A,MSG_DISABLE_RULE=0x0B,MSG_GET_RULES=0x0C,MSG_COMMAND_DISPENSE=0x0D,
 MSG_STOP_REMOTE_DISPENSE=0x0E,MSG_SET_TANK_CONFIG=0x0F,MSG_GET_TANK_STATUS=0x10,
 MSG_HELLO=0x80,MSG_STATUS=0x81,
 MSG_RX_FRAME_DISPLAY=0x82,MSG_RX_FRAME_MAIN=0x83,MSG_INJECT_QUEUED=0x84,
 MSG_INJECT_SENT=0x85,MSG_ERROR=0x86,MSG_PONG=0x87,MSG_RULE_STATUS=0x88,
 MSG_RULE_MATCH=0x89,MSG_RULE_REPLACED=0x8A,MSG_RULE_ERROR=0x8B,
 MSG_REPLACE_BUILD=0x8C,MSG_REPLACE_TX_BEGIN=0x8D,MSG_REPLACE_TX_END=0x8E,
 MSG_MANUAL_CMD_QUEUED=0x8F,MSG_MANUAL_CMD_TX_BEGIN=0x90,MSG_MANUAL_CMD_TX_END=0x91,
 MSG_DISPENSE_OVERRIDE_STATUS=0x92,MSG_DISPENSE_OVERRIDE_EVENT=0x93,
 MSG_TANK_STATUS=0x94,MSG_TANK_EVENT=0x95
};
static BridgeMode bridgeMode=DIRECT_PASS;
static const uint8_t MAGIC0='Q',MAGIC1='B',VERSION=1,MAX_FRAME=64;
static const uint16_t MAX_CONTROL=96;
static const uint32_t RULE_LEASE_MS=2000UL;

template<uint16_t N> class ByteRing {
 uint8_t d[N]; volatile uint16_t h=0,t=0;
 public:
 bool push(uint8_t v){uint16_t n=(h+1)%N;if(n==t)return false;d[h]=v;h=n;return true;}
 bool pop(uint8_t &v){if(h==t)return false;v=d[t];t=(t+1)%N;return true;}
 uint16_t size()const{return h>=t?h-t:N-t+h;}
 uint16_t freeSpace()const{return N-1-size();}
};
template<typename T,uint8_t N> class RecordQueue {
 T d[N];uint8_t h=0,t=0;
 public:
 bool push(const T&v){uint8_t n=(h+1)%N;if(n==t)return false;d[h]=v;h=n;return true;}
 T*front(){return h==t?NULL:&d[t];} void pop(){if(h!=t)t=(t+1)%N;}
 uint8_t size()const{return h>=t?h-t:N-t+h;}
};
struct Capture {uint8_t dir;uint32_t us;uint8_t len,data[MAX_FRAME];};
struct Injection {uint8_t dir;uint16_t seq;uint8_t len,data[MAX_FRAME];};
enum CommandSource:uint8_t { SOURCE_USB=0,SOURCE_GATEWAY=1 };
enum ManualCommandKind:uint8_t { MANUAL_DISPENSE=1 };
struct ManualCommand {
 uint16_t seq,volumeMl,feedPumpParameter;uint32_t queuedMs;
 uint8_t temperature,source,len,kind,data[19];
};
struct Tracker {uint8_t data[MAX_FRAME],len=0,expected=0;uint32_t started=0,last=0;bool held=false;};
struct TxResult {uint16_t written;uint32_t beginUs,endUs;uint8_t targetUart;};

ByteRing<256> toMain,toDisplay;
ByteRing<512> toUsb;
ByteRing<512> toGateway;
RecordQueue<Capture,8> captures;
RecordQueue<Injection,8> injections;
RecordQueue<ManualCommand,4> manualCommands;
Tracker displayTracker,mainTracker;
uint32_t lastRx[2]={0,0},droppedBridge=0,droppedCapture=0;
uint8_t expectedMainLength=0;
enum ExpectedMainKind:uint8_t { EXPECT_NONE=0,EXPECT_A5=1,EXPECT_E5_B2=2,EXPECT_E5_E1=3 };
uint8_t expectedMainKind=EXPECT_NONE;

enum FrameAction:uint8_t { FRAME_PASS=0,FRAME_SUPPRESS=1,FRAME_REPLACE=2 };
enum RuleType:uint8_t { RULE_DISPENSE_VOLUME=1 };
struct ManipulationRule {
 bool configured=false,enabled=false;
 uint8_t type=RULE_DISPENSE_VOLUME,dir=DISPLAY_TO_MAIN,action=FRAME_REPLACE;
 uint16_t overrideValue=0;
 uint32_t matchCount=0,replaceCount=0;
 uint8_t matchPayload[16],lastOriginal[19],lastReplacement[19];
 bool hasOriginal=false,hasReplacement=false;
};
ManipulationRule rule;
uint32_t lastRuleControlMs=0;
struct DispenseOverrideState {
 bool active=false,startSeen=false,mainDispensing=false,targetEventSent=false,aborting=false,abortCcSeen=false;
 uint8_t origin=0;
 uint16_t targetMl=0,originalDisplayMl=0,lastProgressMl=0,feedPumpParameter=0;
 uint32_t startTimestamp=0,refreshCount=0,blockedAbortCount=0,lastA5Ms=0;
 uint8_t controlPayload[16];
};
DispenseOverrideState dispenseOverride;
bool remoteNeedsControlLease=true;
enum DispenseOverrideEvent:uint8_t {
 OVERRIDE_START=1,OVERRIDE_REFRESH_REPLACED=2,OVERRIDE_ABORT_SUPPRESSED=3,
 OVERRIDE_PROGRESS=4,OVERRIDE_TARGET_REACHED=5,OVERRIDE_END=6,OVERRIDE_USER_ABORT=7,
 REMOTE_START=8,REMOTE_DISPLAY_IDLE_SUPPRESSED=9,REMOTE_REFRESH_REPLACED=10,
 REMOTE_PROGRESS=11,REMOTE_COMPLETE=12,REMOTE_ABORT_REQUESTED=13,REMOTE_ABORT_TX=14,
 REMOTE_ABORT_CONFIRMED=15,REMOTE_END=16,REMOTE_ERROR=17,
 REMOTE_PUMP_PARAMETER_REPLACED=21
};
enum DispenseSessionOrigin:uint8_t { ORIGIN_NONE=0,ORIGIN_DISPLAY_OVERRIDE=1,ORIGIN_MANUAL_REMOTE=2 };
static const uint32_t A5_TIMEOUT_MS=1500UL;
static const uint16_t MAX_DISPLAY_DISPENSE_ML=900;
static const uint16_t MAX_MANUAL_DISPENSE_ML=1500;
static const uint16_t MIN_MANUAL_FEED_PUMP_PARAMETER=100;
static const uint16_t MAX_MANUAL_FEED_PUMP_PARAMETER=1200;
struct RuleDecision {FrameAction action;uint8_t length,event;bool matched,remote;uint8_t data[MAX_FRAME];};
void requestRemoteAbort(bool failSafe);
TxResult transmitCompletedFrame(uint8_t dir,const uint8_t*data,uint8_t len);
void observeGatewayB2(const uint8_t*data,uint8_t len);
bool validAA(const uint8_t*data,uint8_t len);
bool validResponse18(const uint8_t*data,uint8_t len);
uint8_t validateGatewayDispense(uint16_t volume,uint8_t temperature);
uint8_t usbDispenseValidationError(uint8_t result);

enum ExternalTankState:uint8_t { EXT_UNKNOWN=0,EXT_LOW=1,EXT_AVAILABLE_PENDING=2,EXT_AVAILABLE_STABLE=3 };
enum TankEvent:uint8_t {
 EXTERNAL_TANK_LOW=1,EXTERNAL_AVAILABLE_PENDING=2,EXTERNAL_AVAILABLE_TIMER_RESET=3,
 EXTERNAL_AVAILABLE_STABLE=4,INTERNAL_TANK_MIN_CHANGED=5,INTERNAL_TANK_MAX_CHANGED=6,
 FILL_STATE_CHANGED=7,EXTERNAL_STOP_LATCHED=8,EXTERNAL_RESET_ARMED=9,
 EXTERNAL_STOP_CLEARED=10
};
struct TankFillState {
 bool delayEnabled=false,rawKnown=false,rawAvailable=false,stableKnown=false;
 bool internalMinKnown=false,internalMinActive=false,internalMaxKnown=false,internalMaxActive=false;
 bool fillActiveKnown=false,fillActive=false,lastFillAllowed=false,hasFrame=false;
 bool externalStopLatched=false,externalResetArmed=false;
 uint8_t externalState=EXT_UNKNOWN,lastFrame[18];
 uint32_t delayMs=0,candidateSinceMs=0,lastValidMs=0,lastStatusReportMs=0;
};
TankFillState tankState;
static const uint32_t TANK_STATUS_TIMEOUT_MS=2000UL;
bool internalTankBelowLow(){return tankState.internalMinKnown&&tankState.internalMinActive;}

// Serial3 gateway envelope: QG, version, type, flags, sequence LE,
// payload-length LE, payload, CRC16-CCITT LE.  It is deliberately independent
// from the USB QB protocol so neither endpoint can accidentally consume the
// other's traffic.
static const uint8_t GW_MAGIC0='Q',GW_MAGIC1='G',GW_VERSION=1,GW_MAX_PAYLOAD=192;
enum GatewayMessageType:uint8_t {
 GW_PING=0x01,GW_GET_STATUS=0x02,GW_DISPENSE=0x03,GW_STOP_DISPENSE=0x04,
 GW_SET_PRESET=0x05,GW_SET_TANK_DELAY=0x06,GW_GET_CONFIG=0x07,
 GW_GET_OVERRIDES=0x08,GW_SET_OVERRIDE=0x09,GW_DELETE_OVERRIDE=0x0A,GW_SAVE_CONFIG=0x0B,
 GW_HELLO=0x80,GW_PONG=0x81,GW_STATUS_SNAPSHOT=0x82,GW_EVENT=0x83,
 GW_COMMAND_RESULT=0x84,GW_CONFIG_RESULT=0x85,GW_ERROR=0x86,GW_RAW_BATCH=0x87
 ,GW_OVERRIDES_RESULT=0x88
};
enum GatewayFrameKind:uint8_t {
 GF_UNKNOWN=0,GF_REQUEST_A5=1,GF_REQUEST_E5_B2=2,GF_REQUEST_E5_E1=3,
 GF_AA00=4,GF_AA_A3=5,GF_AA_E1=6,GF_AA_F1=7,
 GF_RESPONSE_A5=8,GF_RESPONSE_E5_B2=9,GF_RESPONSE_E5_E1=10,GF_CC=11,GF_AA_OTHER=12
};
enum GatewayResult:uint8_t {
 GW_OK=0,GW_BUSY=1,GW_TANK_EMPTY=2,GW_EXTERNAL_TANK_NOT_READY=3,
 GW_TEMP_OUT_OF_RANGE=4,GW_INVALID_VOLUME=5,GW_COMMUNICATION_ERROR=6,
 GW_FILL_ACTIVE=7,GW_FLUSH_ACTIVE=8,GW_INVALID_REQUEST=9,GW_NOT_ACTIVE=10,
 GW_QUEUE_FULL=11,GW_UNSUPPORTED=12,GW_INTERNAL_TANK_BELOW_LOW=13
};
enum GatewayKnownBit:uint8_t {
 GK_A5_D11_BIT4_CYCLIC=0,GK_DISPENSING=1,GK_FILLING=2,GK_FLUSHING=3,GK_HEATING=4,
 GK_PUMP=5,GK_VALVE=6,GK_ERROR=7,GK_TARGET=8,GK_CURRENT=9,GK_REMAINING=10,
 GK_FLOW=11,GK_PROGRESS=12,GK_REQUESTED_TEMP=13,GK_INLET_TEMP=14,
 GK_OUTLET_TEMP=15,GK_INTERNAL_MIN=16,GK_INTERNAL_MAX=17,GK_EXTERNAL_LOW=18,
 GK_EXTERNAL_RAW=19,GK_EXTERNAL_STABLE=20,GK_FILL_ALLOWED=21,GK_FILL_ACTIVE=22,
 GK_AC_MAINS=23,GK_TOTAL_DISPENSED=24,GK_CURRENT_COUNTER=25,GK_FEED_PARAMETER=26,
 GK_MODE=27,GK_ERROR_CODES=28,GK_TDS=29
};
enum GatewayMode:uint8_t { GM_UNKNOWN=0,GM_IDLE=1,GM_RESERVED_2=2,GM_DISPENSE=3,GM_FILL=4,GM_FLUSH=5,GM_HEATING=6,GM_ERROR=7 };
struct GatewayLiveState {
 uint32_t known=0,totalDispensed=0,lastA5Ms=0;
 uint16_t requestedMl=0,currentMl=0,flowCentiMlS=0,errorCodes=0,feedParameter=0;
 uint16_t previousMl=0;uint32_t previousMlMs=0;
 uint8_t requestedTemp=0,inletTemp=0,outletTemp=0,acMains=0,tds=0,tdsCandidate=0,machineFlags=0;
 bool tdsUpdateActive=false;
};
GatewayLiveState gatewayLive;
struct GatewayPreset {bool configured=false;uint16_t volumeMl=0;uint8_t temperature=25;};
GatewayPreset gatewayPresets[8];
struct SemanticVolumeRule {uint8_t configured=0,enabled=0;uint16_t sourceMl=0,targetMl=0;};
SemanticVolumeRule semanticVolumeRules[8];
bool persistentRuleRuntime=false;
static const uint32_t USER_CONFIG_MAGIC=0x514C4346UL;
static const uint8_t USER_CONFIG_VERSION=1;
struct UserConfigRecord {
 uint32_t magic;uint8_t version,tankDelayEnabled;uint32_t tankDelayMs;
 struct {uint8_t configured;uint16_t volumeMl;uint8_t temperature;} presets[8];
 SemanticVolumeRule overrides[8];uint16_t crc;
};
uint16_t userConfigCrc(const UserConfigRecord&record);
void loadUserConfig();
void saveUserConfig();
uint32_t lastGatewaySnapshotMs=0,droppedGateway=0;
static const uint8_t RAW_RING_SIZE=32,RAW_FRAME_MAX=19,RAW_BATCH_MAX_FRAMES=7;
static const uint32_t RAW_BATCH_MAX_AGE_US=50000UL;
struct RawTelemetryFrame {uint32_t timestampUs;uint8_t direction,kind,length,data[RAW_FRAME_MAX];};
class RawTelemetryRing {
 RawTelemetryFrame entries[RAW_RING_SIZE];uint8_t head=0,tail=0,count=0;
 public:
 bool push(const RawTelemetryFrame&frame){if(count>=RAW_RING_SIZE)return false;entries[head]=frame;head=(head+1)%RAW_RING_SIZE;count++;return true;}
 RawTelemetryFrame*at(uint8_t index){return index<count?&entries[(tail+index)%RAW_RING_SIZE]:NULL;}
 void pop(uint8_t amount){while(amount--&&count){tail=(tail+1)%RAW_RING_SIZE;count--;}}
 uint8_t size()const{return count;}
};
RawTelemetryRing rawTelemetry;
uint32_t droppedRawFrames=0;uint16_t rawBatchSequence=0;

static void put16(uint8_t*p,uint16_t v){p[0]=v;p[1]=v>>8;}
static void put32(uint8_t*p,uint32_t v){p[0]=v;p[1]=v>>8;p[2]=v>>16;p[3]=v>>24;}
static uint16_t get16(const uint8_t*p){return p[0]|((uint16_t)p[1]<<8);}
static uint32_t get32(const uint8_t*p){return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static uint16_t crc16(const uint8_t*p,uint16_t n){
 uint16_t c=0xFFFF;while(n--){c^=(uint16_t)*p++<<8;for(uint8_t b=0;b<8;b++)c=c&0x8000?(c<<1)^0x1021:c<<1;}return c;
}
static uint16_t crcContinue(uint16_t c,const uint8_t*p,uint16_t n){
 while(n--){c^=(uint16_t)*p++<<8;for(uint8_t b=0;b<8;b++)c=c&0x8000?(c<<1)^0x1021:c<<1;}return c;
}

bool sendGateway(uint8_t type,uint16_t seq,const uint8_t*payload,uint16_t len){
 if(len>GW_MAX_PAYLOAD)return false;uint16_t total=11+len;
 if(toGateway.freeSpace()<total){droppedGateway++;return false;}
 uint8_t h[9]={GW_MAGIC0,GW_MAGIC1,GW_VERSION,type,0,0,0,0,0};put16(h+5,seq);put16(h+7,len);
 uint16_t c=crcContinue(crc16(h+2,7),payload,len);uint8_t tail[2];put16(tail,c);
 for(uint8_t i=0;i<9;i++)toGateway.push(h[i]);
 for(uint16_t i=0;i<len;i++)toGateway.push(payload[i]);
 toGateway.push(tail[0]);toGateway.push(tail[1]);return true;
}
void sendGatewayResult(uint16_t seq,uint8_t command,uint8_t result){
 uint8_t p[2]={command,result};sendGateway(GW_COMMAND_RESULT,seq,p,sizeof(p));
}
void sendGatewayEvent(uint8_t domain,uint8_t event){
 uint8_t p[6];p[0]=domain;p[1]=event;put32(p+2,millis());sendGateway(GW_EVENT,0,p,sizeof(p));
}

uint8_t gatewayFrameKind(uint8_t dir,const uint8_t*data,uint8_t len,uint8_t expectedKind){
#if QUELLA_GATEWAY_PROTOCOL_STREAM
 if(!len)return GF_UNKNOWN;
 uint8_t kind=GF_UNKNOWN;
 if(dir==DISPLAY_TO_MAIN){
  if(len==1&&data[0]==0xA5)kind=GF_REQUEST_A5;
  else if(len==2&&data[0]==0xE5&&data[1]==0xB2)kind=GF_REQUEST_E5_B2;
  else if(len==2&&data[0]==0xE5&&data[1]==0xE1)kind=GF_REQUEST_E5_E1;
  else if(len==19&&data[0]==0xAA){
   kind=data[16]==0x00?GF_AA00:(data[16]==0xA3?GF_AA_A3:(data[16]==0xE1?GF_AA_E1:(data[16]==0xF1?GF_AA_F1:GF_AA_OTHER)));
  }
 }else{
  if(len==1&&data[0]==0xCC)kind=GF_CC;
  else if(len==18&&expectedKind==EXPECT_A5)kind=GF_RESPONSE_A5;
  else if(len==18&&expectedKind==EXPECT_E5_B2)kind=GF_RESPONSE_E5_B2;
  else if(len==18&&expectedKind==EXPECT_E5_E1)kind=GF_RESPONSE_E5_E1;
 }
 return kind;
#else
 (void)dir;(void)data;(void)len;(void)expectedKind;return GF_UNKNOWN;
#endif
}

void queueRawTelemetry(uint8_t dir,const uint8_t*data,uint8_t len,uint32_t completedUs,uint8_t expectedKind){
#if QUELLA_GATEWAY_PROTOCOL_STREAM
 if(!len||len>RAW_FRAME_MAX){droppedRawFrames++;return;}
 RawTelemetryFrame frame;frame.timestampUs=completedUs;frame.direction=dir;
 frame.kind=gatewayFrameKind(dir,data,len,expectedKind);frame.length=len;memcpy(frame.data,data,len);
 if(!rawTelemetry.push(frame))droppedRawFrames++;
#else
 (void)dir;(void)data;(void)len;(void)completedUs;(void)expectedKind;
#endif
}

void serviceRawTelemetry(){
#if QUELLA_GATEWAY_PROTOCOL_STREAM
 RawTelemetryFrame*oldest=rawTelemetry.at(0);if(!oldest)return;
 if(rawTelemetry.size()<RAW_BATCH_MAX_FRAMES&&(uint32_t)(micros()-oldest->timestampUs)<RAW_BATCH_MAX_AGE_US)return;
 uint8_t payload[GW_MAX_PAYLOAD];payload[0]=1;put16(payload+1,rawBatchSequence+1);payload[3]=0;put32(payload+4,droppedRawFrames);
 uint16_t used=8;uint8_t count=0;
 while(count<RAW_BATCH_MAX_FRAMES){
  RawTelemetryFrame*frame=rawTelemetry.at(count);if(!frame)break;
  uint16_t needed=7+frame->length;if(used+needed>GW_MAX_PAYLOAD)break;
  payload[used++]=frame->direction;payload[used++]=frame->kind;put32(payload+used,frame->timestampUs);used+=4;
  payload[used++]=frame->length;memcpy(payload+used,frame->data,frame->length);used+=frame->length;count++;
 }
 if(!count||toGateway.freeSpace()<11+used)return;
 payload[3]=count;
 if(sendGateway(GW_RAW_BATCH,0,payload,used)){rawBatchSequence++;rawTelemetry.pop(count);}
#endif
}

bool sendControl(uint8_t type,uint16_t seq,const uint8_t*payload,uint16_t len){
 uint16_t total=11+len;if(toUsb.freeSpace()<total)return false;
 uint8_t h[9]={MAGIC0,MAGIC1,VERSION,type,0,0,0,0,0};put16(h+5,seq);put16(h+7,len);
 uint16_t c=crcContinue(crc16(h+2,7),payload,len);uint8_t tail[2];put16(tail,c);
 for(uint8_t i=0;i<9;i++)toUsb.push(h[i]);
 for(uint16_t i=0;i<len;i++)toUsb.push(payload[i]);
 toUsb.push(tail[0]);toUsb.push(tail[1]);return true;
}
bool ruleActive(){return rule.configured&&rule.enabled;}
bool semanticOverridesActive(){
 for(uint8_t i=0;i<8;i++)if(semanticVolumeRules[i].configured&&semanticVolumeRules[i].enabled)return true;
 return false;
}
bool selectSemanticVolumeRule(const uint8_t*data,uint8_t len){
 if(!validAA(data,len)||data[16]!=0x00||data[2]!=0x01)return false;
 uint16_t source=((uint16_t)data[4]<<8)|data[5];
 for(uint8_t i=0;i<8;i++){
  SemanticVolumeRule&r=semanticVolumeRules[i];
  if(!r.configured||!r.enabled||r.sourceMl!=source)continue;
  rule=ManipulationRule();rule.configured=true;rule.enabled=true;rule.type=RULE_DISPENSE_VOLUME;
  rule.dir=DISPLAY_TO_MAIN;rule.action=FRAME_REPLACE;rule.overrideValue=r.targetMl;
  memcpy(rule.matchPayload,data+1,16);persistentRuleRuntime=true;return true;
 }
 if(persistentRuleRuntime){rule=ManipulationRule();persistentRuleRuntime=false;}
 return false;
}
uint16_t userConfigCrc(const UserConfigRecord&record){
 return crc16((const uint8_t*)&record,offsetof(UserConfigRecord,crc));
}
void loadUserConfig(){
 UserConfigRecord stored;EEPROM.get(0,stored);
 if(stored.magic!=USER_CONFIG_MAGIC||stored.version!=USER_CONFIG_VERSION||stored.crc!=userConfigCrc(stored))return;
 // The former availability timer was replaced by an explicit operator-reset
 // latch. Keep the EEPROM fields only for binary record compatibility.
 tankState.delayEnabled=false;tankState.delayMs=0;
 for(uint8_t i=0;i<8;i++){
  gatewayPresets[i].configured=stored.presets[i].configured!=0;
  gatewayPresets[i].volumeMl=stored.presets[i].volumeMl;
  gatewayPresets[i].temperature=stored.presets[i].temperature;
  semanticVolumeRules[i]=stored.overrides[i];
  if(semanticVolumeRules[i].sourceMl<1||semanticVolumeRules[i].sourceMl>MAX_DISPLAY_DISPENSE_ML||
     semanticVolumeRules[i].targetMl<1||semanticVolumeRules[i].targetMl>MAX_MANUAL_DISPENSE_ML)
   semanticVolumeRules[i]=SemanticVolumeRule();
 }
}
void saveUserConfig(){
 UserConfigRecord stored;memset(&stored,0,sizeof(stored));stored.magic=USER_CONFIG_MAGIC;
 stored.version=USER_CONFIG_VERSION;stored.tankDelayEnabled=0;stored.tankDelayMs=0;
 for(uint8_t i=0;i<8;i++){
  stored.presets[i].configured=gatewayPresets[i].configured;stored.presets[i].volumeMl=gatewayPresets[i].volumeMl;
  stored.presets[i].temperature=gatewayPresets[i].temperature;stored.overrides[i]=semanticVolumeRules[i];
 }
 stored.crc=userConfigCrc(stored);EEPROM.put(0,stored);
}
bool validAA(const uint8_t*data,uint8_t len){
 if(len!=19||data[0]!=0xAA)return false;
 uint16_t sum=0;for(uint8_t i=1;i<=16;i++)sum+=data[i];
 return data[17]==(uint8_t)(sum>>8)&&data[18]==(uint8_t)sum;
}
bool validResponse18(const uint8_t*data,uint8_t len){
 if(len!=18)return false;uint16_t sum=0;for(uint8_t i=0;i<16;i++)sum+=data[i];
 return data[16]==(uint8_t)(sum>>8)&&data[17]==(uint8_t)sum;
}
void packDispenseOverride(uint8_t*p){
 p[0]=dispenseOverride.active;p[1]=dispenseOverride.origin;p[2]=dispenseOverride.aborting;
 put16(p+3,dispenseOverride.targetMl);put16(p+5,dispenseOverride.originalDisplayMl);
 put32(p+7,dispenseOverride.startTimestamp);p[11]=dispenseOverride.startSeen;
 put32(p+12,dispenseOverride.refreshCount);put32(p+16,dispenseOverride.blockedAbortCount);
 put16(p+20,dispenseOverride.lastProgressMl);p[22]=dispenseOverride.mainDispensing;
 put32(p+23,dispenseOverride.startSeen?(uint32_t)(micros()-dispenseOverride.startTimestamp):0);
}
void sendDispenseOverrideStatus(uint16_t seq){uint8_t p[27];packDispenseOverride(p);sendControl(MSG_DISPENSE_OVERRIDE_STATUS,seq,p,sizeof(p));}
void sendDispenseOverrideEvent(uint8_t event,const uint8_t*original=NULL){
 uint8_t p[47];memset(p,0,sizeof(p));p[0]=event;packDispenseOverride(p+1);
 if(original)memcpy(p+28,original,19);sendControl(MSG_DISPENSE_OVERRIDE_EVENT,0,p,sizeof(p));
 sendGatewayEvent(1,event);
}
void sendRuleStatus(uint16_t seq){
 uint8_t p[53];memset(p,0,sizeof(p));
 p[0]=rule.configured;p[1]=rule.enabled;p[2]=rule.dir;p[3]=rule.action;p[4]=rule.type;
 put16(p+5,rule.overrideValue);put32(p+7,rule.matchCount);put32(p+11,rule.replaceCount);
 if(rule.hasOriginal)memcpy(p+15,rule.lastOriginal,19);
 if(rule.hasReplacement)memcpy(p+34,rule.lastReplacement,19);
 sendControl(MSG_RULE_STATUS,seq,p,sizeof(p));
}
bool fillAllowed(){
 return !tankState.externalStopLatched&&tankState.externalState==EXT_AVAILABLE_STABLE&&tankState.stableKnown&&
  tankState.internalMaxKnown&&!tankState.internalMaxActive;
}
uint32_t tankDelayRemaining(){
 if(tankState.externalState!=EXT_AVAILABLE_PENDING)return 0;
 uint32_t elapsed=(uint32_t)(millis()-tankState.candidateSinceMs);
 return elapsed>=tankState.delayMs?0:tankState.delayMs-elapsed;
}
void packTankStatus(uint8_t*p){
 memset(p,0,33);p[0]=tankState.externalState;
 if(tankState.rawKnown)p[1]|=0x01;if(tankState.rawAvailable)p[1]|=0x02;
 if(tankState.stableKnown)p[1]|=0x04;if(tankState.externalState==EXT_AVAILABLE_STABLE)p[1]|=0x08;
 if(tankState.internalMinKnown)p[1]|=0x10;if(tankState.internalMinActive)p[1]|=0x20;
 if(tankState.internalMaxKnown)p[1]|=0x40;if(tankState.internalMaxActive)p[1]|=0x80;
 if(fillAllowed())p[2]|=0x01;if(tankState.fillActiveKnown)p[2]|=0x02;
 if(tankState.fillActive)p[2]|=0x04;if(tankState.delayEnabled)p[2]|=0x08;
 if(tankState.externalStopLatched)p[2]|=0x10;if(tankState.externalResetArmed)p[2]|=0x20;
 put32(p+3,tankState.delayMs);put32(p+7,tankDelayRemaining());
 put32(p+11,tankState.rawKnown?(uint32_t)(millis()-tankState.lastValidMs):0xFFFFFFFFUL);
 if(tankState.hasFrame)memcpy(p+15,tankState.lastFrame,18);
}
void sendTankStatus(uint16_t seq){uint8_t p[33];packTankStatus(p);sendControl(MSG_TANK_STATUS,seq,p,sizeof(p));}
void sendTankEvent(uint8_t event){
 uint8_t p[38];p[0]=event;put32(p+1,micros());packTankStatus(p+5);sendControl(MSG_TANK_EVENT,0,p,sizeof(p));
 sendGatewayEvent(2,event);
}
void updateFillAllowedEvent(){
 bool allowed=fillAllowed();if(allowed!=tankState.lastFillAllowed){tankState.lastFillAllowed=allowed;sendTankEvent(FILL_STATE_CHANGED);}
}
void invalidateTankState(bool reportReset){
 bool hadCandidate=tankState.externalState==EXT_AVAILABLE_PENDING;
 tankState.externalState=tankState.externalStopLatched?EXT_LOW:EXT_UNKNOWN;
 tankState.rawKnown=false;tankState.rawAvailable=false;tankState.externalResetArmed=false;
 tankState.stableKnown=false;tankState.internalMinKnown=false;tankState.internalMaxKnown=false;
 tankState.fillActiveKnown=false;tankState.candidateSinceMs=0;tankState.lastValidMs=0;
 gatewayLive.machineFlags&=~((1<<2)|(1<<5)|(1<<6));
 gatewayLive.known&=~((1UL<<GK_FILLING)|(1UL<<GK_PUMP)|(1UL<<GK_VALVE)|(1UL<<GK_INTERNAL_MIN)|
  (1UL<<GK_INTERNAL_MAX)|(1UL<<GK_EXTERNAL_LOW)|(1UL<<GK_EXTERNAL_RAW)|(1UL<<GK_EXTERNAL_STABLE)|
  (1UL<<GK_FILL_ALLOWED)|(1UL<<GK_FILL_ACTIVE)|(1UL<<GK_TDS));
 if(reportReset&&(hadCandidate||tankState.lastFillAllowed))sendTankEvent(EXTERNAL_AVAILABLE_TIMER_RESET);
 updateFillAllowedEvent();sendTankStatus(0);
}
void observeTankB2(const uint8_t*data){
 uint32_t now=millis();bool oldMin=tankState.internalMinActive,oldMinKnown=tankState.internalMinKnown;
 bool oldMax=tankState.internalMaxActive,oldMaxKnown=tankState.internalMaxKnown;
 bool oldFill=tankState.fillActive,oldFillKnown=tankState.fillActiveKnown;
 bool rawAvailable=(data[4]&0x01)==0; // confirmed: 0=available, 1=empty
 memcpy(tankState.lastFrame,data,18);tankState.hasFrame=true;tankState.lastValidMs=now;
 tankState.rawKnown=true;tankState.rawAvailable=rawAvailable;
 tankState.internalMinKnown=true;tankState.internalMinActive=(data[4]&0x08)!=0; // BELOW LOW
 tankState.internalMaxKnown=true;tankState.internalMaxActive=(data[4]&0x20)==0; // ABOVE HIGH
 tankState.fillActiveKnown=true;tankState.fillActive=(data[11]&0x04)!=0;
 observeGatewayB2(data,18);
 if(!rawAvailable){
  bool reset=tankState.externalState==EXT_AVAILABLE_PENDING;
  bool changed=!tankState.externalStopLatched;
  tankState.externalStopLatched=true;tankState.externalResetArmed=false;
  tankState.externalState=EXT_LOW;tankState.stableKnown=true;tankState.candidateSinceMs=0;
  if(reset)sendTankEvent(EXTERNAL_AVAILABLE_TIMER_RESET);
  if(changed){sendTankEvent(EXTERNAL_TANK_LOW);sendTankEvent(EXTERNAL_STOP_LATCHED);}
 }else if(tankState.externalStopLatched){
  // Physical water availability alone never clears the stop. Continue to
  // report LOW to the display until the deliberate START -> IDLE handshake.
  tankState.externalState=EXT_LOW;tankState.stableKnown=true;tankState.candidateSinceMs=0;
 }else{
  bool changed=tankState.externalState!=EXT_AVAILABLE_STABLE;
  tankState.externalState=EXT_AVAILABLE_STABLE;tankState.stableKnown=true;tankState.candidateSinceMs=0;
  if(changed)sendTankEvent(EXTERNAL_AVAILABLE_STABLE);
 }
 if(!oldMinKnown||oldMin!=tankState.internalMinActive)sendTankEvent(INTERNAL_TANK_MIN_CHANGED);
 if(!oldMaxKnown||oldMax!=tankState.internalMaxActive)sendTankEvent(INTERNAL_TANK_MAX_CHANGED);
 if(!oldFillKnown||oldFill!=tankState.fillActive)sendTankEvent(FILL_STATE_CHANGED);
 updateFillAllowedEvent();sendTankStatus(0);
 if(internalTankBelowLow()&&dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&
    dispenseOverride.active&&!dispenseOverride.aborting)requestRemoteAbort(true);
}
void serviceTankState(){
 uint32_t now=millis();
 if(tankState.rawKnown&&(uint32_t)(now-tankState.lastValidMs)>TANK_STATUS_TIMEOUT_MS){
  invalidateTankState(true);return;
 }
}
bool deferrableProcessActiveForExternalStop(){
 if(!gatewayLive.lastA5Ms||(uint32_t)(millis()-gatewayLive.lastA5Ms)>A5_TIMEOUT_MS)return false;
 // Dispense/flush/heating may finish from the internal tank. Never mask an
 // empty external tank for FILL itself; that remains an immediate safety stop.
 const uint8_t deferrableMask=(1<<1)|(1<<3)|(1<<4);
 return (gatewayLive.machineFlags&deferrableMask)||
  (dispenseOverride.active&&!dispenseOverride.aborting);
}
void routeTankB2(const uint8_t*data,uint8_t len){
 if(!validResponse18(data,len)||data[15]!=0xB2){invalidateTankState(true);transmitCompletedFrame(MAIN_TO_DISPLAY,data,len);return;}
 observeTankB2(data);
 if(!tankState.externalStopLatched){transmitCompletedFrame(MAIN_TO_DISPLAY,data,len);return;}
 uint8_t blocked[18];memcpy(blocked,data,18);
 if(deferrableProcessActiveForExternalStop())blocked[4]&=(uint8_t)~0x01;
 else blocked[4]|=0x01;
 uint16_t sum=0;for(uint8_t i=0;i<16;i++)sum+=blocked[i];blocked[16]=(uint8_t)(sum>>8);blocked[17]=(uint8_t)sum;
 transmitCompletedFrame(MAIN_TO_DISPLAY,blocked,18);
}
void endDispenseOverride(bool disableRule){
 if(!dispenseOverride.active&&!dispenseOverride.aborting){if(disableRule)rule.enabled=false;return;}
 uint8_t endEvent=dispenseOverride.origin==ORIGIN_MANUAL_REMOTE?REMOTE_END:OVERRIDE_END;
 dispenseOverride.active=false;dispenseOverride.aborting=false;dispenseOverride.mainDispensing=false;
 if(disableRule)rule.enabled=false;
 remoteNeedsControlLease=true;
 sendDispenseOverrideEvent(endEvent);sendDispenseOverrideStatus(0);
}
void failSafeDispenseOverride(){
 if(!dispenseOverride.active&&!dispenseOverride.aborting)return;
 if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE){
  // START may already have reached Main before the first authoritative A5 was
  // observed. In that uncertainty an abort is safer than assuming Main idle.
  if((dispenseOverride.mainDispensing||dispenseOverride.startSeen)&&!dispenseOverride.aborting){
   requestRemoteAbort(true);return;
  }
  sendDispenseOverrideEvent(REMOTE_ERROR);endDispenseOverride(false);return;
 }
 endDispenseOverride(true);sendRuleStatus(0);
}
bool sameDispenseControlFamily(const uint8_t*data){
 const uint8_t*reference=dispenseOverride.origin==ORIGIN_MANUAL_REMOTE?dispenseOverride.controlPayload:rule.matchPayload;
 if(data[1]!=reference[0]||data[16]!=0x00)return false;
 if(dispenseOverride.origin==ORIGIN_DISPLAY_OVERRIDE&&
    (((uint16_t)data[4]<<8)|data[5])!=dispenseOverride.originalDisplayMl)return false;
 // D1 is the runtime command and D3/D4 is the volume. Every other payload
 // byte must remain identical to the exact configured START family.
 for(uint8_t payloadOffset=0;payloadOffset<16;payloadOffset++){
  if(payloadOffset==1||payloadOffset==3||payloadOffset==4)continue;
  if(data[1+payloadOffset]!=reference[payloadOffset])return false;
 }
 return true;
}
void buildDispenseReplacement(const uint8_t*data,uint8_t*out){
 memcpy(out,data,19);out[4]=(uint8_t)(dispenseOverride.targetMl>>8);out[5]=(uint8_t)dispenseOverride.targetMl;
 uint16_t sum=0;for(uint8_t i=1;i<=16;i++)sum+=out[i];out[17]=(uint8_t)(sum>>8);out[18]=(uint8_t)sum;
}
void buildFeedPumpReplacement(const uint8_t*data,uint8_t*out){
 memcpy(out,data,19);
 // AA E1: raw offsets 3..4 are payload Data[2..3], big-endian ml/min.
 out[3]=(uint8_t)(dispenseOverride.feedPumpParameter>>8);
 out[4]=(uint8_t)dispenseOverride.feedPumpParameter;
 uint16_t sum=0;for(uint8_t i=1;i<=16;i++)sum+=out[i];out[17]=(uint8_t)(sum>>8);out[18]=(uint8_t)sum;
}
void sendRuleMatch(const uint8_t*original){
 uint8_t p[23];put32(p,rule.matchCount);memcpy(p+4,original,19);
 sendControl(MSG_RULE_MATCH,0,p,sizeof(p));
}
void sendRuleReplaced(const uint8_t*original,const uint8_t*replacement){
 uint8_t p[42];put32(p,rule.replaceCount);memcpy(p+4,original,19);memcpy(p+23,replacement,19);
 sendControl(MSG_RULE_REPLACED,0,p,sizeof(p));
}
void sendReplaceBuild(uint8_t dir,const uint8_t*original,uint8_t originalLen,const uint8_t*replacement,uint8_t replacementLen){
 uint8_t p[45];p[0]=dir;p[1]=originalLen;p[2]=replacementLen;
 p[3]=original[17];p[4]=original[18];p[5]=replacement[17];p[6]=replacement[18];
 memcpy(p+7,original,19);memcpy(p+26,replacement,19);sendControl(MSG_REPLACE_BUILD,0,p,sizeof(p));
}
void sendReplaceTxBegin(uint8_t dir,const uint8_t*frame,uint8_t len,uint32_t lastRxUs,const TxResult&tx){
 uint8_t p[30];p[0]=dir;p[1]=tx.targetUart;p[2]=len;put32(p+3,lastRxUs);put32(p+7,tx.beginUs);
 memcpy(p+11,frame,19);sendControl(MSG_REPLACE_TX_BEGIN,0,p,sizeof(p));
}
void sendReplaceTxEnd(uint8_t dir,uint8_t len,const TxResult&tx){
 uint8_t p[13];p[0]=dir;p[1]=tx.targetUart;p[2]=len;put16(p+3,tx.written);
 put32(p+5,tx.beginUs);put32(p+9,tx.endUs);sendControl(MSG_REPLACE_TX_END,0,p,sizeof(p));
}
void buildKnownDispenseFrame(uint16_t volumeMl,uint8_t temperature,uint8_t*frame){
 memset(frame,0,19);frame[0]=0xAA;frame[2]=0x01;frame[3]=temperature;
 frame[4]=(uint8_t)(volumeMl>>8);frame[5]=(uint8_t)volumeMl;
 uint16_t sum=0;for(uint8_t i=1;i<=16;i++)sum+=frame[i];
 frame[17]=(uint8_t)(sum>>8);frame[18]=(uint8_t)sum;
}
void buildKnownAbortFrame(uint8_t*frame){
 // Confirmed Display AA00 abort family: D1=00, 25 C, retained 150 ml preset.
 buildKnownDispenseFrame(150,25,frame);frame[2]=0x00;
 uint16_t sum=0;for(uint8_t i=1;i<=16;i++)sum+=frame[i];
 frame[17]=(uint8_t)(sum>>8);frame[18]=(uint8_t)sum;
}
static const uint8_t EXTERNAL_STOP_START_SUPPRESSED=23;
static const uint8_t EXTERNAL_STOP_RESET_CONFIRM=24;
bool idleForExternalStopReset(){
 if(!gatewayLive.lastA5Ms||(uint32_t)(millis()-gatewayLive.lastA5Ms)>A5_TIMEOUT_MS)return false;
 const uint8_t activeMask=(1<<1)|(1<<2)|(1<<3)|(1<<4)|(1<<7);
 return !(gatewayLive.machineFlags&activeMask)&&
  !(tankState.fillActiveKnown&&tankState.fillActive)&&
  !dispenseOverride.active&&!dispenseOverride.aborting;
}
void clearExternalStopLatch(){
 if(!tankState.externalStopLatched)return;
 tankState.externalStopLatched=false;tankState.externalResetArmed=false;
 tankState.externalState=EXT_AVAILABLE_STABLE;tankState.stableKnown=true;tankState.candidateSinceMs=0;
 sendTankEvent(EXTERNAL_STOP_CLEARED);sendTankEvent(EXTERNAL_AVAILABLE_STABLE);
 updateFillAllowedEvent();sendTankStatus(0);sendGatewayStatus(0);
}
void sendManualQueued(const ManualCommand&command){
 uint8_t p[23];p[0]=command.kind;put16(p+1,command.volumeMl);p[3]=command.len;memcpy(p+4,command.data,19);
 sendControl(MSG_MANUAL_CMD_QUEUED,command.seq,p,sizeof(p));
}
void sendManualTxBegin(const ManualCommand&command,const TxResult&tx){
 uint8_t p[28];p[0]=command.kind;put16(p+1,command.volumeMl);p[3]=tx.targetUart;p[4]=command.len;
 put32(p+5,tx.beginUs);memcpy(p+9,command.data,19);sendControl(MSG_MANUAL_CMD_TX_BEGIN,command.seq,p,sizeof(p));
}
void sendManualTxEnd(const ManualCommand&command,const TxResult&tx){
 uint8_t p[15];p[0]=command.kind;put16(p+1,command.volumeMl);p[3]=tx.targetUart;p[4]=command.len;
 put16(p+5,tx.written);put32(p+7,tx.beginUs);put32(p+11,tx.endUs);
 sendControl(MSG_MANUAL_CMD_TX_END,command.seq,p,sizeof(p));
}
RuleDecision evaluateCompletedFrame(uint8_t dir,const uint8_t*data,uint8_t len){
 RuleDecision decision;decision.action=FRAME_PASS;decision.length=0;decision.event=0;decision.matched=false;decision.remote=false;
 // Fail-safe order matters: uncertain framing/checksum/direction/type is PASS.
 if(dir!=DISPLAY_TO_MAIN)return decision;
 if(tankState.externalStopLatched&&validAA(data,len)&&data[16]==0x00){
  if(data[2]==0x01){
   // START is an acknowledgement request while latched and must never reach
   // Main. Arm only after water returned and the previous process is IDLE.
   if(tankState.rawKnown&&tankState.rawAvailable&&idleForExternalStopReset()&&
      !tankState.externalResetArmed){
    tankState.externalResetArmed=true;sendTankEvent(EXTERNAL_RESET_ARMED);sendTankStatus(0);
   }
   decision.action=FRAME_SUPPRESS;decision.event=EXTERNAL_STOP_START_SUPPRESSED;
   decision.remote=true;return decision;
  }
  if(data[2]==0x00&&tankState.externalResetArmed&&tankState.rawKnown&&
     tankState.rawAvailable&&idleForExternalStopReset()){
   // The second press returned the display to IDLE. The caller first passes
   // this original abort frame and clears only the external latch after TX.
   decision.action=FRAME_PASS;decision.event=EXTERNAL_STOP_RESET_CONFIRM;
   decision.remote=true;return decision;
  }
 }
 if(internalTankBelowLow()&&validAA(data,len)&&data[16]==0x00&&data[2]==0x01){
  decision.action=FRAME_SUPPRESS;decision.event=22;decision.remote=true;return decision;
 }
 bool remoteActive=dispenseOverride.active&&dispenseOverride.origin==ORIGIN_MANUAL_REMOTE;
 if(remoteActive){
  decision.remote=true;
  if(!validAA(data,len)){failSafeDispenseOverride();return decision;}
  if(dispenseOverride.feedPumpParameter&&data[16]==0xE1){
   decision.matched=true;decision.action=FRAME_REPLACE;decision.length=19;
   decision.event=REMOTE_PUMP_PARAMETER_REPLACED;
   buildFeedPumpReplacement(data,decision.data);return decision;
  }
  if(data[2]==0x80&&sameDispenseControlFamily(data)){
   if(!dispenseOverride.originalDisplayMl)dispenseOverride.originalDisplayMl=((uint16_t)data[4]<<8)|data[5];
   decision.matched=true;decision.action=FRAME_REPLACE;decision.length=19;decision.event=REMOTE_REFRESH_REPLACED;
   buildDispenseReplacement(data,decision.data);return decision;
  }
  if(data[2]==0x00&&sameDispenseControlFamily(data)&&dispenseOverride.mainDispensing&&
     dispenseOverride.lastProgressMl<dispenseOverride.targetMl){
   if(!dispenseOverride.originalDisplayMl)dispenseOverride.originalDisplayMl=((uint16_t)data[4]<<8)|data[5];
   decision.matched=true;decision.action=FRAME_SUPPRESS;decision.event=REMOTE_DISPLAY_IDLE_SUPPRESSED;return decision;
  }
  return decision;
 }
 if((!ruleActive()||persistentRuleRuntime)&&!dispenseOverride.active&&!dispenseOverride.aborting&&
    validAA(data,len)&&data[16]==0x00&&data[2]==0x01)
  selectSemanticVolumeRule(data,len);
 if(!ruleActive()||dir!=rule.dir||rule.type!=RULE_DISPENSE_VOLUME)return decision;
 if(!validAA(data,len)){if(dispenseOverride.active||dispenseOverride.aborting)failSafeDispenseOverride();return decision;}
 FrameAction configuredAction=(FrameAction)rule.action;
 if(configuredAction!=FRAME_REPLACE){
  if(data[2]!=0x01||data[16]!=0x00||memcmp(data+1,rule.matchPayload,16)!=0)return decision;
  decision.matched=true;decision.action=configuredAction;return decision;
 }
 if(dispenseOverride.aborting)return decision;
 if(!dispenseOverride.active){
  if(data[2]!=0x01||data[16]!=0x00||memcmp(data+1,rule.matchPayload,16)!=0)return decision;
  dispenseOverride=DispenseOverrideState();dispenseOverride.active=true;dispenseOverride.startSeen=true;
  dispenseOverride.origin=ORIGIN_DISPLAY_OVERRIDE;
  dispenseOverride.targetMl=rule.overrideValue;
  dispenseOverride.originalDisplayMl=((uint16_t)data[4]<<8)|data[5];
  dispenseOverride.startTimestamp=micros();dispenseOverride.lastA5Ms=millis();
  decision.event=OVERRIDE_START;
 }else if(data[2]==0x80&&sameDispenseControlFamily(data)){
  decision.event=OVERRIDE_REFRESH_REPLACED;
 }else if(data[2]==0x00&&sameDispenseControlFamily(data)){
  if(dispenseOverride.origin==ORIGIN_DISPLAY_OVERRIDE){
   // Safety wins over the override target. Mark inactive before forwarding so
   // no later refresh can be replaced, then report only after physical TX.
   dispenseOverride.active=false;dispenseOverride.aborting=true;dispenseOverride.abortCcSeen=false;
   decision.matched=true;decision.action=FRAME_PASS;decision.event=OVERRIDE_USER_ABORT;return decision;
  }
  if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&dispenseOverride.mainDispensing&&
     dispenseOverride.lastProgressMl<dispenseOverride.targetMl){
   decision.matched=true;decision.action=FRAME_SUPPRESS;decision.event=OVERRIDE_ABORT_SUPPRESSED;return decision;
  }
  return decision;
 }else return decision;
 decision.matched=true;decision.action=FRAME_REPLACE;decision.length=19;
 buildDispenseReplacement(data,decision.data);
 return decision;
}
template<uint16_t N> void drain(ByteRing<N>&ring,HardwareSerial&out);
TxResult transmitCompletedFrame(uint8_t dir,const uint8_t*data,uint8_t len){
 HardwareSerial&uart=dir==DISPLAY_TO_MAIN?Serial2:Serial1;
 ByteRing<256>&pending=dir==DISPLAY_TO_MAIN?toMain:toDisplay;
 // Preserve ordering with any already queued direct-pass bytes, then measure
 // the complete physical frame transmission rather than only ring admission.
 drain(pending,uart);uart.flush();
 TxResult result;result.targetUart=dir==DISPLAY_TO_MAIN?2:1;result.beginUs=micros();
 result.written=(uint16_t)uart.write(data,len);uart.flush();result.endUs=micros();return result;
}
void requestRemoteAbort(bool failSafe){
 if(dispenseOverride.origin!=ORIGIN_MANUAL_REMOTE||
    (!dispenseOverride.active&&!dispenseOverride.aborting))return;
 if(dispenseOverride.aborting)return;
 dispenseOverride.active=false;dispenseOverride.aborting=true;dispenseOverride.abortCcSeen=false;
 dispenseOverride.lastA5Ms=millis();
 sendDispenseOverrideEvent(REMOTE_ABORT_REQUESTED);
 uint8_t abortFrame[19];buildKnownAbortFrame(abortFrame);
 TxResult tx=transmitCompletedFrame(DISPLAY_TO_MAIN,abortFrame,19);
 if(tx.written==19){expectedMainLength=1;expectedMainKind=EXPECT_NONE;
  sendDispenseOverrideEvent(REMOTE_ABORT_TX,abortFrame);sendDispenseOverrideStatus(0);return;}
 dispenseOverride.aborting=false;dispenseOverride.mainDispensing=false;
 sendDispenseOverrideEvent(REMOTE_ERROR,abortFrame);sendDispenseOverrideEvent(REMOTE_END);sendDispenseOverrideStatus(0);
}
void routeInterceptedFrame(uint8_t dir,const uint8_t*data,uint8_t len,uint32_t lastRxUs){
 if(dir==DISPLAY_TO_MAIN&&bridgeMode==DISPLAY_ONLY_TEST)return;
 RuleDecision decision=evaluateCompletedFrame(dir,data,len);
 if(decision.matched&&!decision.remote){rule.matchCount++;rule.hasOriginal=true;memcpy(rule.lastOriginal,data,19);}
 if(decision.action==FRAME_SUPPRESS){
  if(decision.event==22){uint8_t e=14;sendControl(MSG_ERROR,0,&e,1);sendGatewayEvent(1,22);}
  if(decision.event==EXTERNAL_STOP_START_SUPPRESSED)sendGatewayEvent(2,EXTERNAL_STOP_START_SUPPRESSED);
  if(decision.matched&&!decision.remote)sendRuleMatch(data);
  if(decision.event==OVERRIDE_ABORT_SUPPRESSED){dispenseOverride.blockedAbortCount++;sendDispenseOverrideEvent(decision.event);}
  if(decision.event==REMOTE_DISPLAY_IDLE_SUPPRESSED){dispenseOverride.blockedAbortCount++;sendDispenseOverrideEvent(decision.event,data);}
  return;
 }
 const uint8_t*output=decision.action==FRAME_REPLACE?decision.data:data;
 uint8_t outputLength=decision.action==FRAME_REPLACE?decision.length:len;
 TxResult tx=transmitCompletedFrame(dir,output,outputLength);
 // USB diagnostics are produced after TX so they cannot delay the local UART.
 if(decision.matched&&!decision.remote)sendRuleMatch(data);
 if(decision.action==FRAME_REPLACE){
  sendReplaceBuild(dir,data,len,decision.data,decision.length);
  sendReplaceTxBegin(dir,decision.data,decision.length,lastRxUs,tx);
  sendReplaceTxEnd(dir,decision.length,tx);
  if(tx.written==decision.length){
   if(decision.remote){dispenseOverride.refreshCount++;}
   else{rule.replaceCount++;rule.hasReplacement=true;memcpy(rule.lastReplacement,decision.data,19);
    if(decision.event==OVERRIDE_REFRESH_REPLACED)dispenseOverride.refreshCount++;
    sendRuleReplaced(data,decision.data);}}
  else{uint8_t e=4;sendControl(MSG_RULE_ERROR,0,&e,1);failSafeDispenseOverride();}
  if(tx.written==decision.length&&decision.event)sendDispenseOverrideEvent(decision.event);
 }
 if(decision.action==FRAME_PASS&&decision.event==OVERRIDE_USER_ABORT){
  sendDispenseOverrideEvent(OVERRIDE_USER_ABORT,data);sendDispenseOverrideStatus(0);
 }
 if(decision.action==FRAME_PASS&&decision.event==EXTERNAL_STOP_RESET_CONFIRM&&tx.written==outputLength)
  clearExternalStopLatch();
}
void observeA5(const uint8_t*data,uint8_t len){
 if(!dispenseOverride.active&&!dispenseOverride.aborting)return;
 if(!validResponse18(data,len)){failSafeDispenseOverride();return;}
 uint16_t progress=((uint16_t)data[3]<<8)|data[4];bool dispensing=data[1]==0x01;
 bool changed=progress!=dispenseOverride.lastProgressMl||dispensing!=dispenseOverride.mainDispensing;
 dispenseOverride.lastProgressMl=progress;dispenseOverride.mainDispensing=dispensing;dispenseOverride.lastA5Ms=millis();
 if(changed)sendDispenseOverrideEvent(
  dispenseOverride.origin==ORIGIN_MANUAL_REMOTE?REMOTE_PROGRESS:OVERRIDE_PROGRESS
 );
 if(!dispenseOverride.aborting&&progress>=dispenseOverride.targetMl&&!dispenseOverride.targetEventSent){
  dispenseOverride.targetEventSent=true;sendDispenseOverrideEvent(OVERRIDE_TARGET_REACHED);
 }
 if(!dispensing){
  if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE){
   if(dispenseOverride.aborting){
    if(dispenseOverride.abortCcSeen){sendDispenseOverrideEvent(REMOTE_ABORT_CONFIRMED);endDispenseOverride(false);}
   }else if(progress>=dispenseOverride.targetMl){sendDispenseOverrideEvent(REMOTE_COMPLETE);endDispenseOverride(false);}
   else failSafeDispenseOverride();
  }
  else if(dispenseOverride.aborting||progress>=dispenseOverride.targetMl)endDispenseOverride(false);
  else failSafeDispenseOverride();
 }
}

void observeGatewayAA(const uint8_t*data,uint8_t len){
 if(!validAA(data,len))return;
 if(data[16]==0x00){
  gatewayLive.requestedTemp=data[3];gatewayLive.requestedMl=((uint16_t)data[4]<<8)|data[5];
  gatewayLive.known|=(1UL<<GK_REQUESTED_TEMP);
 }
}
void observeGatewayA5(const uint8_t*data,uint8_t len){
 if(!validResponse18(data,len))return;uint32_t now=millis();uint16_t current=((uint16_t)data[3]<<8)|data[4];
 bool dispensing=(data[1]&0x01)!=0,d11Bit4=(data[11]&0x10)!=0,flushing=(data[11]&0x02)!=0;
 bool heating=(data[11]&0x60)!=0||(data[13]&0x0F)!=0,error=data[9]!=0||data[10]!=0;
 gatewayLive.machineFlags=(gatewayLive.machineFlags&((1<<2)|(1<<5)|(1<<6)))|
  (d11Bit4?1:0)|(dispensing?(1<<1):0)|(flushing?(1<<3):0)|(heating?(1<<4):0)|(error?(1<<7):0);
 gatewayLive.errorCodes=((uint16_t)data[9]<<8)|data[10];gatewayLive.currentMl=current;
 gatewayLive.inletTemp=data[12];gatewayLive.acMains=data[14];
 gatewayLive.totalDispensed=((uint32_t)data[5]<<24)|((uint32_t)data[6]<<16)|((uint32_t)data[7]<<8)|data[8];
 gatewayLive.known|=(1UL<<GK_A5_D11_BIT4_CYCLIC)|(1UL<<GK_DISPENSING)|(1UL<<GK_FLUSHING)|(1UL<<GK_HEATING)|
  (1UL<<GK_ERROR)|(1UL<<GK_ERROR_CODES)|(1UL<<GK_CURRENT)|(1UL<<GK_CURRENT_COUNTER)|
  (1UL<<GK_INLET_TEMP)|(1UL<<GK_AC_MAINS)|(1UL<<GK_TOTAL_DISPENSED)|(1UL<<GK_MODE);
 if(dispensing&&(gatewayLive.known&(1UL<<GK_REQUESTED_TEMP))&&gatewayLive.requestedTemp>25){
  gatewayLive.outletTemp=data[2];gatewayLive.known|=1UL<<GK_OUTLET_TEMP;
 }else gatewayLive.known&=~(1UL<<GK_OUTLET_TEMP);
 if(dispensing&&gatewayLive.previousMlMs&&current>=gatewayLive.previousMl){
  uint32_t dt=now-gatewayLive.previousMlMs;if(dt>=100&&dt<=5000){
   gatewayLive.flowCentiMlS=(uint16_t)min(65535UL,((uint32_t)(current-gatewayLive.previousMl)*100000UL)/dt);
   gatewayLive.known|=(1UL<<GK_FLOW);
  }
 }else if(!dispensing){gatewayLive.known&=~(1UL<<GK_FLOW);gatewayLive.flowCentiMlS=0;}
 gatewayLive.previousMl=current;gatewayLive.previousMlMs=now;gatewayLive.lastA5Ms=now;
}
void observeGatewayB2(const uint8_t*data,uint8_t len){
 if(!validResponse18(data,len)||data[15]!=0xB2)return;
 bool fill=(data[11]&0x04)!=0,valve=(data[11]&0x02)!=0;
 bool tdsUpdate=(data[11]&0x10)!=0,tdsMeasurement=(data[13]&0x01)!=0;
 if(tdsUpdate||tdsMeasurement)gatewayLive.tdsCandidate=data[3];
 if((gatewayLive.tdsUpdateActive&&!tdsUpdate)||(tdsMeasurement&&!(gatewayLive.known&(1UL<<GK_TDS)))){
  gatewayLive.tds=gatewayLive.tdsCandidate;gatewayLive.known|=1UL<<GK_TDS;
 }
 gatewayLive.tdsUpdateActive=tdsUpdate;
 gatewayLive.machineFlags=(gatewayLive.machineFlags&~((1<<2)|(1<<5)|(1<<6)))|
  (fill?(1<<2)|(1<<5):0)|(valve?(1<<6):0);
 gatewayLive.known|=(1UL<<GK_FILLING)|(1UL<<GK_PUMP)|(1UL<<GK_VALVE)|(1UL<<GK_INTERNAL_MIN)|
  (1UL<<GK_INTERNAL_MAX)|(1UL<<GK_EXTERNAL_LOW)|(1UL<<GK_EXTERNAL_RAW)|
  (1UL<<GK_EXTERNAL_STABLE)|(1UL<<GK_FILL_ALLOWED)|(1UL<<GK_FILL_ACTIVE)|(1UL<<GK_MODE);
}
void observeGatewayE1(const uint8_t*data,uint8_t len){
 if(!validResponse18(data,len)||data[15]!=0xE1)return;
 gatewayLive.feedParameter=((uint16_t)data[1]<<8)|data[2];gatewayLive.known|=(1UL<<GK_FEED_PARAMETER);
}
uint8_t gatewayMode(uint32_t known){
 if(!(known&(1UL<<GK_MODE)))return GM_UNKNOWN;
 if((known&(1UL<<GK_ERROR))&&(gatewayLive.machineFlags&(1<<7)))return GM_ERROR;
 if((known&(1UL<<GK_DISPENSING))&&(gatewayLive.machineFlags&(1<<1)))return GM_DISPENSE;
 if((known&(1UL<<GK_FILLING))&&(gatewayLive.machineFlags&(1<<2)))return GM_FILL;
 if((known&(1UL<<GK_FLUSHING))&&(gatewayLive.machineFlags&(1<<3)))return GM_FLUSH;
 if((known&(1UL<<GK_HEATING))&&(gatewayLive.machineFlags&(1<<4)))return GM_HEATING;
 return GM_IDLE;
}
void packGatewayStatus(uint8_t*p){
 memset(p,0,46);p[0]=1;put32(p+1,millis());uint32_t known=gatewayLive.known;
 if(!gatewayLive.lastA5Ms||(uint32_t)(millis()-gatewayLive.lastA5Ms)>TANK_STATUS_TIMEOUT_MS)
  known&=~((1UL<<GK_A5_D11_BIT4_CYCLIC)|(1UL<<GK_DISPENSING)|(1UL<<GK_FLUSHING)|(1UL<<GK_HEATING)|
   (1UL<<GK_ERROR)|(1UL<<GK_ERROR_CODES)|(1UL<<GK_CURRENT)|(1UL<<GK_CURRENT_COUNTER)|(1UL<<GK_FLOW)|
   (1UL<<GK_PROGRESS)|(1UL<<GK_REMAINING)|(1UL<<GK_INLET_TEMP)|(1UL<<GK_OUTLET_TEMP)|
   (1UL<<GK_AC_MAINS)|(1UL<<GK_TOTAL_DISPENSED));
 if(tankState.internalMinKnown)known|=1UL<<GK_INTERNAL_MIN;
 if(tankState.internalMaxKnown)known|=1UL<<GK_INTERNAL_MAX;
 if(tankState.rawKnown)known|=(1UL<<GK_EXTERNAL_LOW)|(1UL<<GK_EXTERNAL_RAW);
 if(tankState.stableKnown)known|=1UL<<GK_EXTERNAL_STABLE;
 if(tankState.fillActiveKnown)known|=1UL<<GK_FILL_ACTIVE;
 if((dispenseOverride.active||dispenseOverride.aborting)||gatewayLive.requestedMl)
  known|=1UL<<GK_TARGET;
 if((known&(1UL<<GK_CURRENT))&&(known&(1UL<<GK_TARGET)))
  known|=(1UL<<GK_REMAINING)|(1UL<<GK_PROGRESS);
 else known&=~((1UL<<GK_REMAINING)|(1UL<<GK_PROGRESS));
 if(!(known&((1UL<<GK_DISPENSING)|(1UL<<GK_FILLING)|(1UL<<GK_FLUSHING)|(1UL<<GK_HEATING)|(1UL<<GK_ERROR))))known|=1UL<<GK_MODE;
 put32(p+5,known);p[9]=gatewayMode(known);p[10]=gatewayLive.machineFlags;p[11]=dispenseOverride.origin;
 p[12]=(dispenseOverride.origin==ORIGIN_DISPLAY_OVERRIDE&&dispenseOverride.active?1:0)|
  (dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&(dispenseOverride.active||dispenseOverride.aborting)?2:0)|
  (dispenseOverride.aborting?4:0);
 put16(p+13,gatewayLive.errorCodes);
 uint16_t target=(dispenseOverride.active||dispenseOverride.aborting)?dispenseOverride.targetMl:gatewayLive.requestedMl;
 put16(p+15,target);put16(p+17,gatewayLive.currentMl);
 uint16_t remaining=target>gatewayLive.currentMl?target-gatewayLive.currentMl:0;put16(p+19,remaining);
 put16(p+21,gatewayLive.flowCentiMlS);
 uint16_t progress=target?min(1000UL,(uint32_t)gatewayLive.currentMl*1000UL/target):0;put16(p+23,progress);
 p[25]=gatewayLive.requestedTemp;p[26]=gatewayLive.inletTemp;p[27]=gatewayLive.outletTemp;
 p[28]=(tankState.internalMinActive?1:0)|(tankState.internalMaxActive?2:0)|
  (tankState.rawKnown&&!tankState.rawAvailable?4:0)|(tankState.rawAvailable?8:0)|
  (tankState.externalState==EXT_AVAILABLE_STABLE?16:0);
 p[29]=(fillAllowed()?1:0)|(tankState.fillActive?2:0)|
  (tankState.externalStopLatched?8:0)|(tankState.externalResetArmed?16:0);
 put32(p+30,tankDelayRemaining());p[34]=gatewayLive.acMains;put32(p+35,gatewayLive.totalDispensed);
 put16(p+39,gatewayLive.currentMl);put16(p+41,gatewayLive.feedParameter);put16(p+43,gatewayLive.requestedMl);
 p[45]=gatewayLive.tds;
}
void sendGatewayStatus(uint16_t seq){uint8_t p[46];packGatewayStatus(p);sendGateway(GW_STATUS_SNAPSHOT,seq,p,sizeof(p));}
void sendGatewayConfig(uint16_t seq){
  uint8_t p[49];memset(p,0,sizeof(p));p[0]=2;p[1]=0;put32(p+2,0);put16(p+6,MAX_MANUAL_DISPENSE_ML);p[8]=8;
 for(uint8_t i=0;i<8;i++){uint8_t*e=p+9+i*5;e[0]=i;e[1]=gatewayPresets[i].configured;
  put16(e+2,gatewayPresets[i].volumeMl);e[4]=gatewayPresets[i].temperature;}
 sendGateway(GW_CONFIG_RESULT,seq,p,sizeof(p));
}
void sendGatewayOverrides(uint16_t seq){
 uint8_t p[58];memset(p,0,sizeof(p));p[0]=1;p[1]=8;
 for(uint8_t i=0;i<8;i++){uint8_t*e=p+2+i*7;e[0]=i;e[1]=semanticVolumeRules[i].configured;
  e[2]=semanticVolumeRules[i].enabled;put16(e+3,semanticVolumeRules[i].sourceMl);put16(e+5,semanticVolumeRules[i].targetMl);}
 sendGateway(GW_OVERRIDES_RESULT,seq,p,sizeof(p));
}
void queueCapture(uint8_t dir,const uint8_t*data,uint8_t len,uint32_t us){
 if(!sniffEnabled||!len)return;Capture r;r.dir=dir;r.us=us;r.len=min(len,MAX_FRAME);memcpy(r.data,data,r.len);
 if(!captures.push(r))droppedCapture++;
}
void completeFrame(Tracker&t,uint8_t dir){
 if(!t.len)return;uint8_t completedKind=dir==MAIN_TO_DISPLAY?expectedMainKind:EXPECT_NONE;
 queueCapture(dir,t.data,t.len,t.started);
 queueRawTelemetry(dir,t.data,t.len,t.last,completedKind);
 if(t.held){
  if(dir==MAIN_TO_DISPLAY&&completedKind==EXPECT_E5_B2)routeTankB2(t.data,t.len);
  else routeInterceptedFrame(dir,t.data,t.len,t.last);
 }
 if(dir==DISPLAY_TO_MAIN){
  if(t.data[0]==0xAA&&t.len==19)observeGatewayAA(t.data,t.len);
  if(t.data[0]==0xA5){expectedMainLength=18;expectedMainKind=EXPECT_A5;}
  else if(t.data[0]==0xE5&&t.len==2){expectedMainLength=18;expectedMainKind=t.data[1]==0xB2?EXPECT_E5_B2:EXPECT_E5_E1;}
  else if(t.data[0]==0xAA&&t.len==19){expectedMainLength=1;expectedMainKind=EXPECT_NONE;}
 }else if(t.len==expectedMainLength){
  expectedMainLength=0;expectedMainKind=EXPECT_NONE;
  if(dispenseOverride.aborting&&t.len==1&&t.data[0]==0xCC)dispenseOverride.abortCcSeen=true;
  if(completedKind==EXPECT_A5){observeGatewayA5(t.data,t.len);observeA5(t.data,t.len);}
  if(completedKind==EXPECT_E5_E1)observeGatewayE1(t.data,t.len);
 }
 else if(dir==MAIN_TO_DISPLAY&&expectedMainKind!=EXPECT_NONE){
  if(expectedMainKind==EXPECT_A5)failSafeDispenseOverride();
  if(expectedMainKind==EXPECT_E5_B2)invalidateTankState(true);
  expectedMainLength=0;expectedMainKind=EXPECT_NONE;
 }
 t.len=t.expected=0;t.held=false;
}
void trackByte(Tracker&t,uint8_t dir,uint8_t v,uint32_t now){
 if(!t.len){t.started=now;t.held=((ruleActive()||semanticOverridesActive())&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
   (internalTankBelowLow()&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
   (tankState.externalStopLatched&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
   (dispenseOverride.active&&dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
   (dir==MAIN_TO_DISPLAY&&expectedMainKind==EXPECT_E5_B2);if(dir==DISPLAY_TO_MAIN){
   if(v==0xA5)t.expected=1;else if(v==0xE5)t.expected=2;else if(v==0xAA)t.expected=19;else t.expected=0;
  }else{t.expected=expectedMainLength;if(v==0xCC)t.expected=1;}}
 t.last=now;if(t.len<MAX_FRAME)t.data[t.len++]=v;else{completeFrame(t,dir);return;}
 if(t.expected&&t.len>=t.expected)completeFrame(t,dir);
}
void flushIdle(Tracker&t,uint8_t dir,uint32_t now){if(t.len&&(uint32_t)(now-t.last)>=idleGapUs)completeFrame(t,dir);}
template<uint16_t N> void drain(ByteRing<N>&ring,HardwareSerial&out){while(ring.size()&&out.availableForWrite()){uint8_t v;if(!ring.pop(v))break;out.write(v);}}
void receive(HardwareSerial&in,ByteRing<256>&out,Tracker&t,uint8_t dir,bool forward){
 while(in.available()){uint8_t v=(uint8_t)in.read();uint32_t now=micros();lastRx[dir]=now;
    bool hold=t.len?t.held:(((ruleActive()||semanticOverridesActive())&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
      (internalTankBelowLow()&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
      (tankState.externalStopLatched&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
      (dispenseOverride.active&&dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&dir==DISPLAY_TO_MAIN&&v==0xAA)||
      (dir==MAIN_TO_DISPLAY&&expectedMainKind==EXPECT_E5_B2));
    if(forward&&!hold&&!out.push(v))droppedBridge++;trackByte(t,dir,v,now);}
}
bool safeToInject(uint32_t now){return !displayTracker.len&&!mainTracker.len&&(uint32_t)(now-lastRx[0])>=idleGapUs&&(uint32_t)(now-lastRx[1])>=idleGapUs;}
void serviceManualCommand(uint32_t now){
 ManualCommand*command=manualCommands.front();if(!command)return;
 if(command->source==SOURCE_GATEWAY&&(uint32_t)(millis()-command->queuedMs)>1500UL){
  sendGatewayResult(command->seq,GW_DISPENSE,GW_BUSY);manualCommands.pop();return;}
 if(!safeToInject(now))return;
 uint8_t validation=validateGatewayDispense(command->volumeMl,command->temperature);
 if(validation!=GW_OK){
  if(command->source==SOURCE_GATEWAY)sendGatewayResult(command->seq,GW_DISPENSE,validation);
  else{uint8_t e=usbDispenseValidationError(validation);sendControl(MSG_ERROR,command->seq,&e,1);}
  manualCommands.pop();return;
 }
 if(bridgeMode!=DIRECT_PASS||ruleActive()||dispenseOverride.active||dispenseOverride.aborting){
  if(command->source==SOURCE_GATEWAY)sendGatewayResult(command->seq,GW_DISPENSE,GW_BUSY);
  else{uint8_t e=6;sendControl(MSG_ERROR,command->seq,&e,1);}manualCommands.pop();return;}
 TxResult tx=transmitCompletedFrame(DISPLAY_TO_MAIN,command->data,command->len);
 sendManualTxBegin(*command,tx);sendManualTxEnd(*command,tx);
 if(tx.written!=command->len){
  if(command->source==SOURCE_GATEWAY)sendGatewayResult(command->seq,GW_DISPENSE,GW_COMMUNICATION_ERROR);
  else{uint8_t e=7;sendControl(MSG_ERROR,command->seq,&e,1);}manualCommands.pop();failSafeDispenseOverride();return;
 }
 expectedMainLength=1;expectedMainKind=EXPECT_NONE;
 dispenseOverride=DispenseOverrideState();dispenseOverride.active=true;dispenseOverride.startSeen=true;
 dispenseOverride.origin=ORIGIN_MANUAL_REMOTE;dispenseOverride.targetMl=command->volumeMl;
 dispenseOverride.feedPumpParameter=command->feedPumpParameter;
 remoteNeedsControlLease=command->source==SOURCE_USB;
 dispenseOverride.startTimestamp=micros();dispenseOverride.lastA5Ms=millis();
 memcpy(dispenseOverride.controlPayload,command->data+1,16);
 gatewayLive.requestedMl=command->volumeMl;gatewayLive.requestedTemp=command->temperature;
 gatewayLive.known|=(1UL<<GK_REQUESTED_TEMP);sendDispenseOverrideEvent(REMOTE_START,command->data);
 if(command->source==SOURCE_GATEWAY)sendGatewayResult(command->seq,GW_DISPENSE,GW_OK);
 manualCommands.pop();
}
void serviceInjection(uint32_t now){
 Injection*r=injections.front();if(!r||!safeToInject(now))return;
 ByteRing<256>&target=r->dir==DISPLAY_TO_MAIN?toMain:toDisplay;if(target.freeSpace()<r->len)return;
 for(uint8_t i=0;i<r->len;i++)target.push(r->data[i]);uint8_t p[3];put16(p,r->seq);p[2]=r->dir;
 sendControl(MSG_INJECT_SENT,r->seq,p,3);injections.pop();
}
void serviceReports(){
 Capture*r=captures.front();if(!r)return;uint8_t p[6+MAX_FRAME];put32(p,r->us);put16(p+4,r->len);memcpy(p+6,r->data,r->len);
 if(sendControl(r->dir==DISPLAY_TO_MAIN?MSG_RX_FRAME_DISPLAY:MSG_RX_FRAME_MAIN,0,p,6+r->len))captures.pop();
}
void sendStatus(uint16_t seq){uint8_t p[16];p[0]=bridgeMode;p[1]=sniffEnabled;p[2]=injections.size();p[3]=captures.size();
 put32(p+4,quellaBaud);put32(p+8,droppedBridge);put32(p+12,droppedCapture);sendControl(MSG_STATUS,seq,p,16);}

void applyTankConfig(bool enabled,uint32_t delay){
 // Compatibility endpoint only. The former timer is retired; one LOW sample
 // now creates an explicit operator-reset latch.
 (void)enabled;(void)delay;tankState.delayEnabled=false;tankState.delayMs=0;
}

void handleControl(uint8_t type,uint16_t seq,const uint8_t*p,uint16_t len){
 lastRuleControlMs=millis();
 bool mutatesBridgeOrRule=type==MSG_SET_CONFIG||type==MSG_SET_BRIDGE_MODE||type==MSG_SET_RULE||
  type==MSG_CLEAR_RULE||type==MSG_ENABLE_RULE||type==MSG_DISABLE_RULE;
 if(mutatesBridgeOrRule&&dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&
    (dispenseOverride.active||dispenseOverride.aborting)){
  if(dispenseOverride.active)requestRemoteAbort(true);
  uint8_t e=9;sendControl(MSG_ERROR,seq,&e,1);return;
 }
 if(type==MSG_SET_CONFIG&&len==10){
  bridgeMode=p[0]==DISPLAY_ONLY_TEST?DISPLAY_ONLY_TEST:DIRECT_PASS;uint32_t baud=get32(p+1);
  idleGapUs=constrain(get32(p+5),1000UL,100000UL);sniffEnabled=p[9]!=0;
  if(baud>=1200&&baud<=1000000&&baud!=quellaBaud){quellaBaud=baud;Serial1.flush();Serial2.flush();Serial1.end();Serial2.end();Serial1.begin(baud);Serial2.begin(baud);}sendStatus(seq);
 }else if(type==MSG_START_SNIFF){sniffEnabled=true;sendStatus(seq);}
 else if(type==MSG_STOP_SNIFF){sniffEnabled=false;sendStatus(seq);}
 else if(type==MSG_SET_BRIDGE_MODE&&len==1){bridgeMode=p[0]==DISPLAY_ONLY_TEST?DISPLAY_ONLY_TEST:DIRECT_PASS;sendStatus(seq);}
 else if((type==MSG_INJECT_DISPLAY_TO_MAIN||type==MSG_INJECT_MAIN_TO_DISPLAY)&&len&&len<=MAX_FRAME){
  Injection r;r.dir=type==MSG_INJECT_DISPLAY_TO_MAIN?DISPLAY_TO_MAIN:MAIN_TO_DISPLAY;r.seq=seq;r.len=len;memcpy(r.data,p,len);
  if(injections.push(r)){uint8_t reply[3];put16(reply,seq);reply[2]=injections.size();sendControl(MSG_INJECT_QUEUED,seq,reply,3);}
  else{uint8_t e=1;sendControl(MSG_ERROR,seq,&e,1);}
 }else if(type==MSG_PING)sendControl(MSG_PONG,seq,p,len);
 else if(type==MSG_COMMAND_DISPENSE&&(len==2||len==4)){
  uint16_t volume=get16(p);
  if(volume<1||volume>MAX_MANUAL_DISPENSE_ML){uint8_t e=5;sendControl(MSG_ERROR,seq,&e,1);}
  else if(len==4&&(get16(p+2)<MIN_MANUAL_FEED_PUMP_PARAMETER||
                   get16(p+2)>MAX_MANUAL_FEED_PUMP_PARAMETER)){
   uint8_t e=13;sendControl(MSG_ERROR,seq,&e,1);
  }
  else if(!tankState.internalMinKnown){uint8_t e=15;sendControl(MSG_ERROR,seq,&e,1);}
  else if(tankState.internalMinActive){uint8_t e=14;sendControl(MSG_ERROR,seq,&e,1);}
  else if(bridgeMode!=DIRECT_PASS||ruleActive()||dispenseOverride.active||dispenseOverride.aborting){uint8_t e=6;sendControl(MSG_ERROR,seq,&e,1);}
  else{ManualCommand command;memset(&command,0,sizeof(command));command.seq=seq;command.volumeMl=volume;command.queuedMs=millis();command.temperature=25;command.source=SOURCE_USB;command.len=19;command.kind=MANUAL_DISPENSE;
   command.feedPumpParameter=len==4?get16(p+2):0;
   buildKnownDispenseFrame(volume,command.temperature,command.data);
   if(manualCommands.push(command))sendManualQueued(command);
   else{uint8_t e=7;sendControl(MSG_ERROR,seq,&e,1);}}}
 else if(type==MSG_STOP_REMOTE_DISPENSE&&len==0){
  if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&dispenseOverride.active)requestRemoteAbort(false);
  else{uint8_t e=8;sendControl(MSG_ERROR,seq,&e,1);}
 }
 else if(type==MSG_SET_TANK_CONFIG&&len==5){
  uint32_t delay=get32(p+1);
  if(delay>60000UL){uint8_t e=10;sendControl(MSG_ERROR,seq,&e,1);}
  else{applyTankConfig(p[0]!=0,delay);sendTankStatus(seq);}
 }
 else if(type==MSG_GET_TANK_STATUS&&len==0)sendTankStatus(seq);
 else if(type==MSG_SET_RULE){
  endDispenseOverride(false);
  bool valid=len==23;
  if(valid)valid=p[0]==1&&p[1]==RULE_DISPENSE_VOLUME&&p[2]<=MAIN_TO_DISPLAY&&p[3]<=FRAME_REPLACE;
  // The first supported class is a complete AA00 dispense-start payload.
  if(valid)valid=p[8]==0x01&&p[22]==0x00;
  if(!valid){rule.enabled=false;uint8_t e=1;sendControl(MSG_RULE_ERROR,seq,&e,1);sendDispenseOverrideStatus(seq);}
  else{rule=ManipulationRule();rule.configured=true;rule.type=p[1];rule.dir=p[2];rule.action=p[3];
   rule.enabled=p[4]!=0;rule.overrideValue=get16(p+5);memcpy(rule.matchPayload,p+7,16);sendRuleStatus(seq);sendDispenseOverrideStatus(seq);}
 }else if(type==MSG_CLEAR_RULE){endDispenseOverride(false);rule=ManipulationRule();sendRuleStatus(seq);sendDispenseOverrideStatus(seq);}
 else if(type==MSG_ENABLE_RULE){
  if(!rule.configured){uint8_t e=2;sendControl(MSG_RULE_ERROR,seq,&e,1);}
  else{rule.enabled=true;sendRuleStatus(seq);sendDispenseOverrideStatus(seq);}
 }else if(type==MSG_DISABLE_RULE){endDispenseOverride(false);rule.enabled=false;sendRuleStatus(seq);sendDispenseOverrideStatus(seq);}
 else if(type==MSG_GET_RULES){sendRuleStatus(seq);sendDispenseOverrideStatus(seq);}
 else{uint8_t e=2;sendControl(MSG_ERROR,seq,&e,1);}
}

struct ControlParser {uint8_t state=0,h[7],hp=0,p[MAX_CONTROL],cp[2],cpn=0;uint16_t len=0,pos=0;} control;
void resetControl(){control=ControlParser();}
void serviceUsb(){
 while(Serial.available()){uint8_t v=(uint8_t)Serial.read();
  if(control.state==0){if(v==MAGIC0)control.state=1;}
  else if(control.state==1){if(v==MAGIC1){control.state=2;control.hp=0;}else control.state=v==MAGIC0?1:0;}
  else if(control.state==2){control.h[control.hp++]=v;if(control.hp==7){control.len=get16(control.h+5);
    if(control.h[0]!=VERSION||control.len>MAX_CONTROL)resetControl();else{control.pos=control.cpn=0;control.state=control.len?3:4;}}}
  else if(control.state==3){control.p[control.pos++]=v;if(control.pos==control.len)control.state=4;}
  else{control.cp[control.cpn++]=v;if(control.cpn==2){uint8_t tmp[7+MAX_CONTROL];memcpy(tmp,control.h,7);memcpy(tmp+7,control.p,control.len);
    if(crc16(tmp,7+control.len)==get16(control.cp))handleControl(control.h[1],get16(control.h+3),control.p,control.len);
    else if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&
            (dispenseOverride.active||dispenseOverride.aborting))failSafeDispenseOverride();
    resetControl();}}
 }
}

bool validGatewayTemperature(uint8_t temperature){
 return temperature==25||temperature==45||temperature==55||temperature==80||temperature==90||temperature==95;
}
uint8_t validateGatewayDispense(uint16_t volume,uint8_t temperature){
 if(volume<1||volume>MAX_MANUAL_DISPENSE_ML)return GW_INVALID_VOLUME;
 if(!validGatewayTemperature(temperature))return GW_TEMP_OUT_OF_RANGE;
 if(bridgeMode!=DIRECT_PASS||ruleActive()||dispenseOverride.active||dispenseOverride.aborting)return GW_BUSY;
 if(!gatewayLive.lastA5Ms||(uint32_t)(millis()-gatewayLive.lastA5Ms)>A5_TIMEOUT_MS)return GW_COMMUNICATION_ERROR;
 if(!tankState.internalMinKnown)return GW_COMMUNICATION_ERROR;
 if(tankState.internalMinActive)return GW_INTERNAL_TANK_BELOW_LOW;
 if((gatewayLive.known&(1UL<<GK_DISPENSING))&&(gatewayLive.machineFlags&(1<<1)))return GW_BUSY;
 if(tankState.fillActiveKnown&&tankState.fillActive)return GW_FILL_ACTIVE;
 if((gatewayLive.known&(1UL<<GK_FLUSHING))&&(gatewayLive.machineFlags&(1<<3)))return GW_FLUSH_ACTIVE;
 if(tankState.rawKnown&&!tankState.rawAvailable)return GW_TANK_EMPTY;
 if(!tankState.rawKnown||tankState.externalState!=EXT_AVAILABLE_STABLE)return GW_EXTERNAL_TANK_NOT_READY;
 return GW_OK;
}
uint8_t usbDispenseValidationError(uint8_t result){
 if(result==GW_INTERNAL_TANK_BELOW_LOW)return 14;
 if(result==GW_COMMUNICATION_ERROR)return 15;
 return 6;
}
void handleGateway(uint8_t type,uint16_t seq,const uint8_t*p,uint16_t len){
 if(type==GW_PING){sendGateway(GW_PONG,seq,p,len);return;}
 if(type==GW_GET_STATUS&&len==0){sendGatewayStatus(seq);return;}
 if(type==GW_GET_CONFIG&&len==0){sendGatewayConfig(seq);return;}
 if(type==GW_GET_OVERRIDES&&len==0){sendGatewayOverrides(seq);return;}
 if(type==GW_DISPENSE&&len==3){
  uint16_t volume=get16(p);uint8_t temperature=p[2];uint8_t result=validateGatewayDispense(volume,temperature);
  if(result!=GW_OK){sendGatewayResult(seq,type,result);return;}
  ManualCommand command;memset(&command,0,sizeof(command));command.seq=seq;command.volumeMl=volume;command.queuedMs=millis();command.temperature=temperature;
  command.source=SOURCE_GATEWAY;command.len=19;command.kind=MANUAL_DISPENSE;buildKnownDispenseFrame(volume,temperature,command.data);
  if(!manualCommands.push(command)){sendGatewayResult(seq,type,GW_QUEUE_FULL);return;}
  sendManualQueued(command);return; // Result follows physical transmission.
 }
 if(type==GW_STOP_DISPENSE&&len==0){
  if(dispenseOverride.origin!=ORIGIN_MANUAL_REMOTE||!dispenseOverride.active){sendGatewayResult(seq,type,GW_NOT_ACTIVE);return;}
  requestRemoteAbort(false);sendGatewayResult(seq,type,dispenseOverride.aborting?GW_OK:GW_COMMUNICATION_ERROR);return;
 }
 if(type==GW_SET_TANK_DELAY&&len==5){
  uint32_t delay=get32(p+1);if(delay>60000UL){sendGatewayResult(seq,type,GW_INVALID_REQUEST);return;}
  applyTankConfig(p[0]!=0,delay);sendTankStatus(0);sendGatewayResult(seq,type,GW_OK);sendGatewayStatus(0);return;
 }
 if(type==GW_SET_PRESET&&(len==3||len==4)){
  uint8_t id=p[0];uint16_t volume=get16(p+1);uint8_t temperature=len==4?p[3]:25;
  if(id>=8){sendGatewayResult(seq,type,GW_INVALID_REQUEST);return;}
  if(volume<1||volume>MAX_MANUAL_DISPENSE_ML){sendGatewayResult(seq,type,GW_INVALID_VOLUME);return;}
  if(!validGatewayTemperature(temperature)){sendGatewayResult(seq,type,GW_TEMP_OUT_OF_RANGE);return;}
  gatewayPresets[id].configured=true;gatewayPresets[id].volumeMl=volume;gatewayPresets[id].temperature=temperature;
  sendGatewayResult(seq,type,GW_OK);return;
 }
 if(type==GW_SET_OVERRIDE&&len==6){
  uint8_t id=p[0];uint16_t source=get16(p+1),target=get16(p+3);
  if(id>=8||source<1||source>MAX_DISPLAY_DISPENSE_ML||target<1||target>MAX_MANUAL_DISPENSE_ML){sendGatewayResult(seq,type,GW_INVALID_REQUEST);return;}
  semanticVolumeRules[id].configured=1;semanticVolumeRules[id].enabled=p[5]!=0;
  semanticVolumeRules[id].sourceMl=source;semanticVolumeRules[id].targetMl=target;
  sendGatewayResult(seq,type,GW_OK);sendGatewayOverrides(0);return;
 }
 if(type==GW_DELETE_OVERRIDE&&len==1){
  if(p[0]>=8){sendGatewayResult(seq,type,GW_INVALID_REQUEST);return;}
  semanticVolumeRules[p[0]]=SemanticVolumeRule();sendGatewayResult(seq,type,GW_OK);sendGatewayOverrides(0);return;
 }
 if(type==GW_SAVE_CONFIG&&len==0){saveUserConfig();sendGatewayResult(seq,type,GW_OK);return;}
 sendGatewayResult(seq,type,len>GW_MAX_PAYLOAD?GW_INVALID_REQUEST:GW_UNSUPPORTED);
}

struct GatewayParser {uint8_t state=0,h[7],hp=0,p[GW_MAX_PAYLOAD],cp[2],cpn=0;uint16_t len=0,pos=0;} gatewayControl;
void resetGateway(){gatewayControl=GatewayParser();}
void serviceGateway(){
 while(Serial3.available()){uint8_t v=(uint8_t)Serial3.read();
  if(gatewayControl.state==0){if(v==GW_MAGIC0)gatewayControl.state=1;}
  else if(gatewayControl.state==1){if(v==GW_MAGIC1){gatewayControl.state=2;gatewayControl.hp=0;}else gatewayControl.state=v==GW_MAGIC0?1:0;}
  else if(gatewayControl.state==2){gatewayControl.h[gatewayControl.hp++]=v;if(gatewayControl.hp==7){gatewayControl.len=get16(gatewayControl.h+5);
    if(gatewayControl.h[0]!=GW_VERSION||gatewayControl.len>GW_MAX_PAYLOAD)resetGateway();
    else{gatewayControl.pos=gatewayControl.cpn=0;gatewayControl.state=gatewayControl.len?3:4;}}}
  else if(gatewayControl.state==3){gatewayControl.p[gatewayControl.pos++]=v;if(gatewayControl.pos==gatewayControl.len)gatewayControl.state=4;}
  else{gatewayControl.cp[gatewayControl.cpn++]=v;if(gatewayControl.cpn==2){
    uint16_t c=crc16(gatewayControl.h,7);c=crcContinue(c,gatewayControl.p,gatewayControl.len);
    if(c==get16(gatewayControl.cp))handleGateway(gatewayControl.h[1],get16(gatewayControl.h+3),gatewayControl.p,gatewayControl.len);
    else{uint8_t e=GW_COMMUNICATION_ERROR;sendGateway(GW_ERROR,get16(gatewayControl.h+3),&e,1);}resetGateway();}}
 }
}

void setup(){
 loadUserConfig();
 Serial.begin(PC_BAUD);Serial1.begin(quellaBaud);Serial2.begin(quellaBaud);Serial3.begin(GATEWAY_BAUD);
 uint8_t hello[12]={'M','E','G','A','2','5','6','0',0,0,0,0};put32(hello+8,quellaBaud);sendControl(MSG_HELLO,0,hello,12);
 uint8_t gatewayHello[12]={'M','E','G','A','2','5','6','0',GW_VERSION,1,0,0};sendGateway(GW_HELLO,0,gatewayHello,sizeof(gatewayHello));
 sendTankStatus(0);
}
void loop(){
 // UART forwarding always has priority over control parsing and reporting.
 if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&
    (dispenseOverride.active||dispenseOverride.aborting)&&
    (!dispenseOverride.startSeen||dispenseOverride.targetMl<1||
     dispenseOverride.targetMl>MAX_MANUAL_DISPENSE_ML))
  failSafeDispenseOverride();
 if(((ruleActive()&&!persistentRuleRuntime)||((dispenseOverride.active||dispenseOverride.aborting)&&remoteNeedsControlLease))&&
    (uint32_t)(millis()-lastRuleControlMs)>RULE_LEASE_MS){
  if(dispenseOverride.origin==ORIGIN_MANUAL_REMOTE&&(dispenseOverride.active||dispenseOverride.aborting))failSafeDispenseOverride();
  else{endDispenseOverride(false);rule.enabled=false;sendRuleStatus(0);} // PC/GUI disappeared: atomically return to PASS.
 }
 if((dispenseOverride.active||dispenseOverride.aborting)&&
    (uint32_t)(millis()-dispenseOverride.lastA5Ms)>A5_TIMEOUT_MS)failSafeDispenseOverride();
 serviceTankState();
 drain(toMain,Serial2);drain(toDisplay,Serial1);
 receive(Serial1,toMain,displayTracker,DISPLAY_TO_MAIN,bridgeMode==DIRECT_PASS);
 receive(Serial2,toDisplay,mainTracker,MAIN_TO_DISPLAY,true);
 drain(toMain,Serial2);drain(toDisplay,Serial1);
 uint32_t now=micros();flushIdle(displayTracker,DISPLAY_TO_MAIN,now);flushIdle(mainTracker,MAIN_TO_DISPLAY,now);
 serviceManualCommand(now);serviceInjection(now);serviceUsb();serviceGateway();serviceRawTelemetry();serviceReports();
 if((uint32_t)(millis()-lastGatewaySnapshotMs)>=200UL){lastGatewaySnapshotMs=millis();sendGatewayStatus(0);}
 drain(toUsb,Serial);drain(toGateway,Serial3);
}
