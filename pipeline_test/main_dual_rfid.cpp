/*
1. CỤM 2 MÀN HÌNH LCD I2C 16x2
--------------------------------------------------
  - VCC   ->  Nguồn 5V ngoài
  - GND   ->  GND ESP32 + GND nguồn ngoài
  - SDA   ->  D21 ESP32
  - SCL   ->  D22 ESP32

2. CỤM 2 MODULE RFID MFRC522 (Giao tiếp SPI)
--------------------------------------------------
  + DÙNG CHUNG CẢ 2 MODULE:
    - 3.3V  ->  3.3V ESP32
    - GND   ->  GND ESP32
    - RST   ->  D27 ESP32 
    - SCK   ->  D18 ESP32
    - MISO  ->  D19 ESP32
    - MOSI  ->  D23 ESP32
  + CHÂN CHỌN RIÊNG TỪNG MODULE (SS/CS/SDA):
    - SDA/SS (RFID VÀO) ->  D5  ESP32
    - SDA/SS (RFID RA)  ->  D4  ESP32

3. CỤM 2 SERVO SG90 VÀ 2 CÒI CHÍP (BUZZER)
--------------------------------------------------
  + SERVO VÀO (IN)  : Dây cam (Signal) -> D12 ESP32
  + SERVO RA  (OUT) : Dây cam (Signal) -> D14 ESP32
  + BUZZER VÀO (IN) : Chân Dương(+) -> D26 ESP32, Chân Âm(-) -> GND
  + BUZZER RA (OUT) : Chân Dương(+) -> D25 ESP32, Chân Âm(-) -> GND
4. CỤM NHẢ THẺ VÀ CẢM BIẾN ĐỖ XE (HỒNG NGOẠI)
--------------------------------------------------
  + NÚT NHẤN (Button) : Một chân -> D15 ESP32, Chân còn lại -> GND
  + SERVO NHẢ THẺ     : Dây cam (Signal) -> D13 ESP32
  + CẢM BIẾN IR A0    : Chân OUT -> D33 ESP32
  + CẢM BIẾN IR A1    : Chân OUT -> D32 ESP32
  + CẢM BIẾN IR A2    : Chân OUT -> D34 ESP32
======================================================================
*/

#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <ESP32Servo.h>

// ==========================================================
// CẤU HÌNH PINOUT ESP32
// ==========================================================
// 1. RFID
#define SS_PIN_IN   5   
#define SS_PIN_OUT  4   
#define RST_PIN     27  

// 2. LCD I2C
LiquidCrystal_I2C lcd_in(0x27, 16, 2);   // Lối Vào (Chưa hàn A0)
LiquidCrystal_I2C lcd_out(0x26, 16, 2);  // Lối Ra (Đã hàn chập A0)

// 3. Servo SG90
#define SERVO_IN_PIN  12
#define SERVO_OUT_PIN 14
Servo servo_in;
Servo servo_out;

// 4. Buzzer
#define BUZZER_IN_PIN  26
#define BUZZER_OUT_PIN 25

// 5. Card Dispenser (Nha the)
#define BUTTON_PIN    15
#define SERVO_DISPENSER_PIN 13
Servo servo_dispenser;

// 6. IR Parking Sensors
#define IR_A0_PIN     33
#define IR_A1_PIN     32
#define IR_A2_PIN     34

// 7. IR Barrier Sensors (Chống kẹp xe)
#define IR_BARRIER_IN_PIN  36 // Pin SP (SVP)
#define IR_BARRIER_OUT_PIN 39 // Pin SN (SVN)

// Khởi tạo RFID
MFRC522 rfid_in(SS_PIN_IN, RST_PIN);
MFRC522 rfid_out(SS_PIN_OUT, RST_PIN);

// ==========================================================
// BIẾN QUẢN LÝ THỜI GIAN (NON-BLOCKING)
// ==========================================================
unsigned long servoInTimer = 0;
unsigned long servoOutTimer = 0;
unsigned long lcdInTimer = 0;
unsigned long lcdOutTimer = 0;
unsigned long buzzerInTimer = 0;
unsigned long buzzerOutTimer = 0;
unsigned long rfidInCooldown = 0;
unsigned long rfidOutCooldown = 0;

bool isServoInOpen = false;
bool isServoOutOpen = false;
bool carHasEnteredBarrierIn = false;
bool carHasEnteredBarrierOut = false;

bool isBuzzerInOn = false;
bool isBuzzerOutOn = false;

// Bien quan ly cho Dispenser
bool isDispensing = false;
unsigned long dispenserTimer = 0;
int lastButtonState = HIGH;

// Buffer đọc Serial thay thế String (Chống phân mảnh RAM)
char serialBuffer[64];
int serialIdx = 0;

// Bien luu trang thai cho trong hien tai
char lastSpots[16] = "";

void showEmptySpots(bool forceUpdate = false) {
  int irA0 = digitalRead(IR_A0_PIN);
  int irA1 = digitalRead(IR_A1_PIN);
  int irA2 = digitalRead(IR_A2_PIN);
  
  char spots[16] = "";
  if (irA0 == HIGH) strcat(spots, "A0 ");
  if (irA1 == HIGH) strcat(spots, "A1 ");
  if (irA2 == HIGH) strcat(spots, "A2 ");
  
  if (strlen(spots) == 0) {
    strcpy(spots, "FULL");
  }
  
  if (forceUpdate || strcmp(spots, lastSpots) != 0) {
    strcpy(lastSpots, spots);
    lcd_in.clear();
    lcd_in.setCursor(0, 0);
    lcd_in.print("EMPTY: ");
    lcd_in.setCursor(0, 1);
    lcd_in.print(spots);
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  // --- 1. SETUP BUZZER & SENSORS ---
  pinMode(BUZZER_IN_PIN, OUTPUT);
  digitalWrite(BUZZER_IN_PIN, LOW);
  pinMode(BUZZER_OUT_PIN, OUTPUT);
  digitalWrite(BUZZER_OUT_PIN, LOW);
  
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(IR_A0_PIN, INPUT);
  pinMode(IR_A1_PIN, INPUT);
  pinMode(IR_A2_PIN, INPUT);
  
  pinMode(IR_BARRIER_IN_PIN, INPUT);
  pinMode(IR_BARRIER_OUT_PIN, INPUT);

  // --- 2. SETUP LCD ---
  lcd_in.init();
  lcd_in.backlight();
  lcd_in.setCursor(0, 0);
  lcd_in.print("Parking System");
  lcd_in.setCursor(0, 1);
  lcd_in.print("Initializing...");
  
  lcd_out.init();
  lcd_out.backlight();
  lcd_out.setCursor(0, 0);
  lcd_out.print("Parking System");
  lcd_out.setCursor(0, 1);
  lcd_out.print("Initializing...");

  // --- 3. SETUP SERVO ---
  servo_in.attach(SERVO_IN_PIN);
  servo_out.attach(SERVO_OUT_PIN);
  servo_dispenser.attach(SERVO_DISPENSER_PIN);
  servo_in.write(90);  // Đảo chiều ngõ vào: Đóng là 90 độ
  servo_out.write(90); // Đảo chiều ngõ ra: Đóng là 90 độ
  servo_dispenser.write(5);

  // --- 4. SETUP RFID ---
  pinMode(SS_PIN_IN, OUTPUT);
  pinMode(SS_PIN_OUT, OUTPUT);
  digitalWrite(SS_PIN_IN, HIGH);
  digitalWrite(SS_PIN_OUT, HIGH);

  SPI.begin();
  
  digitalWrite(SS_PIN_IN, LOW);
  rfid_in.PCD_Init();
  rfid_in.PCD_SetAntennaGain(rfid_in.RxGain_max);
  digitalWrite(SS_PIN_IN, HIGH);
  delay(10);

  digitalWrite(SS_PIN_OUT, LOW);
  rfid_out.PCD_Init();
  rfid_out.PCD_SetAntennaGain(rfid_out.RxGain_max);
  digitalWrite(SS_PIN_OUT, HIGH);
  delay(10);

  showEmptySpots(true);
  
  lcd_out.clear();
  lcd_out.setCursor(0, 0);
  lcd_out.print("Ready Check Out ");
  lcd_out.setCursor(0, 1);
  lcd_out.print("  <-- EXIT <--");

  // Lưu ý: Timer bắt đầu chạy để 5s sau reset về dòng chữ "Xin moi quet the"
  lcdInTimer = millis();
  lcdOutTimer = millis();

  Serial.println("[*] ESP32 da khoi tao xong.");
}

// Chuyển String thành in trực tiếp ra Serial (Tiết kiệm bộ nhớ)
void printUID(const char* prefix, MFRC522& rfid) {
  Serial.print(prefix);
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) Serial.print("0");
    Serial.print(rfid.uid.uidByte[i], HEX);
  }
  Serial.println();
}

// Bật còi và tự động tắt bằng non-blocking trong loop
void startBuzzerIn(unsigned long duration_ms) {
  digitalWrite(BUZZER_IN_PIN, HIGH);
  isBuzzerInOn = true;
  buzzerInTimer = millis() + duration_ms; 
}

void startBuzzerOut(unsigned long duration_ms) {
  digitalWrite(BUZZER_OUT_PIN, HIGH);
  isBuzzerOutOn = true;
  buzzerOutTimer = millis() + duration_ms; 
}

void displayLCD_In(const char* line1, const char* line2) {
  lcd_in.clear();
  lcd_in.setCursor(0, 0);
  lcd_in.print(line1);
  lcd_in.setCursor(0, 1);
  lcd_in.print(line2);
  lcdInTimer = millis(); 
}

void displayLCD_Out(const char* line1, const char* line2) {
  lcd_out.clear();
  lcd_out.setCursor(0, 0);
  lcd_out.print(line1);
  lcd_out.setCursor(0, 1);
  lcd_out.print(line2);
  lcdOutTimer = millis(); 
}

void parseCommand(const char* cmd) {
  if (strncmp(cmd, "CMD:IN:OPEN", 11) == 0) {
    displayLCD_In("Welcome!", "PLEASE ENTER");
    servo_in.write(0); // Đảo chiều ngõ vào: Mở là 0 độ
    isServoInOpen = true;
    carHasEnteredBarrierIn = false;
    servoInTimer = millis();
  } 
  else if (strncmp(cmd, "CMD:OUT:OPEN:", 13) == 0) {
    const char* price = cmd + 13; 
    char line2[16];
    snprintf(line2, sizeof(line2), "Fee: %s VND", price);
    displayLCD_Out("GOODBYE!", line2);
    servo_out.write(0); // Đảo chiều ngõ ra: Mở là 0 độ
    isServoOutOpen = true;
    carHasEnteredBarrierOut = false;
    servoOutTimer = millis();
  }
  else if (strncmp(cmd, "CMD:IN:DENY:", 12) == 0) {
    const char* msg = cmd + 12;
    displayLCD_In("ACCESS DENIED!", msg);
    startBuzzerIn(500); 
  }
  else if (strncmp(cmd, "CMD:OUT:DENY:", 13) == 0) {
    const char* msg = cmd + 13;
    displayLCD_Out("EXIT DENIED!", msg);
    startBuzzerOut(500); 
  }
}

void processSerialCommands() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      serialBuffer[serialIdx] = '\0'; 
      if (serialIdx > 0) {
        parseCommand(serialBuffer);
      }
      serialIdx = 0; 
    } else if (c != '\r' && serialIdx < sizeof(serialBuffer) - 1) {
      serialBuffer[serialIdx++] = c;
    }
  }
}

void loop() {
  unsigned long currentMillis = millis();

  // --- Logic Dispenser Nhả thẻ tự động (Non-blocking) ---
  int buttonState = digitalRead(BUTTON_PIN);
  if (buttonState == LOW && lastButtonState == HIGH) {
    if (!isDispensing) {
      servo_dispenser.write(70);
      isDispensing = true;
      dispenserTimer = currentMillis;
    }
  }
  lastButtonState = buttonState;

  if (isDispensing && (currentMillis - dispenserTimer > 1000)) {
    servo_dispenser.write(5);
    isDispensing = false;
  }

  // --- Xử lý tự động tắt Buzzer, Servo và xóa màn hình (Non-blocking) ---
  if (isBuzzerInOn && currentMillis >= buzzerInTimer) {
    digitalWrite(BUZZER_IN_PIN, LOW);
    isBuzzerInOn = false;
  }
  if (isBuzzerOutOn && currentMillis >= buzzerOutTimer) {
    digitalWrite(BUZZER_OUT_PIN, LOW);
    isBuzzerOutOn = false;
  }
  
  // --- Xử lý ĐÓNG SERVO NGÕ VÀO ---
  if (isServoInOpen) {
    int irVal = digitalRead(IR_BARRIER_IN_PIN);
    
    if (irVal == LOW) { 
      // Xe đang nằm ngay dưới thanh chắn
      carHasEnteredBarrierIn = true;
      servoInTimer = currentMillis; // Reset lại timer, TUYỆT ĐỐI KHÔNG ĐÓNG kẹp đầu xe
    } 
    else if (irVal == HIGH && carHasEnteredBarrierIn) {
      // Xe đã đi lọt qua khỏi thanh chắn hoàn toàn
      servo_in.write(90);
      isServoInOpen = false;
      carHasEnteredBarrierIn = false;
    }
    else if (irVal == HIGH && !carHasEnteredBarrierIn && (currentMillis - servoInTimer > 10000)) {
      // Đã mở rào 10 giây nhưng xe không thèm chạy qua -> Tự động đóng lại phòng hờ
      servo_in.write(90);
      isServoInOpen = false;
      Serial.println("SYS:ERR:TIMEOUT_IN"); // Báo lỗi lên Pi
    }
  }
  
  // --- Xử lý ĐÓNG SERVO NGÕ RA ---
  if (isServoOutOpen) {
    int irVal = digitalRead(IR_BARRIER_OUT_PIN);
    
    if (irVal == LOW) { 
      // Xe đang nằm ngay dưới thanh chắn
      carHasEnteredBarrierOut = true;
      servoOutTimer = currentMillis; 
    } 
    else if (irVal == HIGH && carHasEnteredBarrierOut) {
      // Xe đã đi lọt qua khỏi thanh chắn hoàn toàn
      servo_out.write(90);
      isServoOutOpen = false;
      carHasEnteredBarrierOut = false;
    }
    else if (irVal == HIGH && !carHasEnteredBarrierOut && (currentMillis - servoOutTimer > 10000)) {
      // Đã mở rào 10 giây nhưng xe không thèm chạy qua -> Tự động đóng lại
      servo_out.write(90);
      isServoOutOpen = false;
      Serial.println("SYS:ERR:TIMEOUT_OUT"); // Báo lỗi lên Pi
    }
  }
  
  if (lcdInTimer > 0 && (currentMillis - lcdInTimer > 5000)) {
    showEmptySpots(true);
    lcdInTimer = 0; // Tắt đếm ngược
  }

  // --- Cập nhật màn hình chỗ trống liên tục mỗi 1 giây (khi rảnh) ---
  static unsigned long lastIrCheck = 0;
  if (lcdInTimer == 0 && (currentMillis - lastIrCheck > 1000)) {
    lastIrCheck = currentMillis;
    showEmptySpots(false);
  }

  if (lcdOutTimer > 0 && (currentMillis - lcdOutTimer > 5000)) {
    lcd_out.clear();
    lcd_out.setCursor(0, 0);
    lcd_out.print("Please SwipeCard");
    lcd_out.setCursor(0, 1);
    lcd_out.print("  <-- EXIT <--");
    lcdOutTimer = 0; // Tắt đếm ngược
  }

  // --- Lắng nghe lệnh từ máy chủ Pi ---
  processSerialCommands();

  // =====================================
  // 1. KIỂM TRA ĐẦU ĐỌC NGÕ VÀO (IN)
  // =====================================
  digitalWrite(SS_PIN_OUT, HIGH); 
  if (currentMillis >= rfidInCooldown && rfid_in.PICC_IsNewCardPresent() && rfid_in.PICC_ReadCardSerial()) {
    startBuzzerIn(1000); 
    printUID("IN:", rfid_in);
    
    rfid_in.PICC_HaltA();
    rfid_in.PCD_StopCrypto1();
    
    rfidInCooldown = currentMillis + 1000; 
  }

  // =====================================
  // 2. KIỂM TRA ĐẦU ĐỌC NGÕ RA (OUT)
  // =====================================
  digitalWrite(SS_PIN_IN, HIGH); 
  if (currentMillis >= rfidOutCooldown && rfid_out.PICC_IsNewCardPresent() && rfid_out.PICC_ReadCardSerial()) {
    startBuzzerOut(1000); 
    printUID("OUT:", rfid_out);
    
    rfid_out.PICC_HaltA();
    rfid_out.PCD_StopCrypto1();

    rfidOutCooldown = currentMillis + 1000; 
  }
}
