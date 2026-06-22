#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>

// ==========================================================
// 1. CẤU HÌNH MẠNG VÀ SERVER (THAY ĐỔI Ở ĐÂY)
// ==========================================================
const char* ssid = "92";
const char* password = "1234567891011";

// ĐÂY LÀ ĐỊA CHỈ IP CỦA MÁY TÍNH (LAPTOP) CHẠY CODE PYTHON
// Port 5000 là port của Flask server.
const char* serverName = "http://100.117.176.106:8000/api/swipe"; 

// ==========================================================
// 2. KHAI BÁO CHÂN RFID RC522
// ==========================================================
#define SS_PIN  5
#define RST_PIN 22
MFRC522 rfid(SS_PIN, RST_PIN);

void setup() {
  Serial.begin(115200);
  delay(1000);

  // Khởi tạo SPI và RFID
  SPI.begin();
  rfid.PCD_Init();
  Serial.println("\n[+] Khoi tao RFID RC522 thanh cong. Dang cho the...");

  // Kết nối WiFi
  WiFi.begin(ssid, password);
  Serial.print("[*] Dang ket noi WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n[+] Da ket noi WiFi!");
  Serial.print("    IP cua ESP32: ");
  Serial.println(WiFi.localIP());
}

void loop() {
  // Kiểm tra xem có thẻ mới đưa vào không
  if (!rfid.PICC_IsNewCardPresent()) return;
  // Kiểm tra xem có đọc được dữ liệu thẻ không
  if (!rfid.PICC_ReadCardSerial()) return;

  // Lấy mã UID của thẻ và chuyển thành chuỗi (String)
  String uidString = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uidString += "0";
    uidString += String(rfid.uid.uidByte[i], HEX);
  }
  uidString.toUpperCase();
  
  Serial.println("=====================================");
  Serial.print("[!] Phat hien the UID: ");
  Serial.println(uidString);

  // Halt thẻ hiện tại để tránh đọc liên tục một lần quẹt
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  // Gửi mã UID lên Python Server qua HTTP POST
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(serverName);
    http.addHeader("Content-Type", "application/json");

    // Tạo cục dữ liệu JSON: {"uid": "XXXXXXXX"}
    String httpRequestData = "{\"rfid_code\":\"" + uidString + "\"}";
    
    Serial.println("[*] Dang gui du lieu len Server...");
    int httpResponseCode = http.POST(httpRequestData);

    // Xử lý phản hồi từ Python trả về
    if (httpResponseCode > 0) {
      Serial.print("[+] Server tra ve ma code: ");
      Serial.println(httpResponseCode);
      String payload = http.getString();
      Serial.println("    Noi dung: " + payload);
    } else {
      Serial.print("[-] Loi gui HTTP POST: ");
      Serial.println(httpResponseCode);
    }
    http.end();
  } else {
    Serial.println("[-] Loi: Mat ket noi WiFi!");
  }
  
  // Trễ 2 giây để tránh việc người dùng để thẻ lâu bị lưu 2-3 lần
  delay(2000); 
}