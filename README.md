# 你畫我猜 — Firebase 版本

## 📁 檔案結構
```
/
├── admin.html          管理員控制台
├── show.html           大螢幕展示頁
├── game.html           參賽者手機頁
├── firebase-config.js  ⚠️ 填入你的 Firebase 設定
└── README.md
```

---

## 🔧 第一步：建立 Firebase 專案

1. 前往 https://console.firebase.google.com
2. 點「新增專案」→ 輸入名稱 → 建立
3. 左側選「Realtime Database」→「建立資料庫」
   - 選擇離你最近的地區（asia-east1 = 台灣）
   - 模式選「**以測試模式啟動**」（方便本地測試）
4. 左側選「專案設定」（⚙️ 圖示）
5. 往下找「你的應用程式」→「新增應用程式」→ 選「</> 網頁」
6. 應用程式暱稱隨意填 → 點「註冊應用程式」
7. 複製 firebaseConfig 物件內容

---

## ✏️ 第二步：填入設定

打開 `firebase-config.js`，將內容換成你的設定：

```js
const FIREBASE_CONFIG = {
  apiKey:            "AIzaSy...",
  authDomain:        "my-game.firebaseapp.com",
  databaseURL:       "https://my-game-default-rtdb.asia-southeast1.firebasedatabase.app",
  projectId:         "my-game",
  storageBucket:     "my-game.appspot.com",
  messagingSenderId: "123456789",
  appId:             "1:123456789:web:abc123"
};
```

---

## 🚀 第三步：本地執行

⚠️ 因為使用 ES Module，不能直接雙擊 HTML 開啟，需要一個簡單的 HTTP server。

### 方法 A：Python（推薦，最簡單）
```bash
# 進入檔案所在資料夾
cd 你的資料夾路徑

# Python 3
python -m http.server 8080

# 然後開啟瀏覽器：
# 管理員：http://localhost:8080/admin.html
# 大螢幕：http://localhost:8080/show.html
# 手機：  http://localhost:8080/game.html
```

### 方法 B：Node.js
```bash
npx serve .
# 或
npx http-server .
```

### 方法 C：VS Code
安裝「Live Server」擴充套件 → 右鍵 admin.html → Open with Live Server

---

## 🌐 第四步：跨網路讓手機加入

本地測試時，手機要連同一個 WiFi，然後輸入電腦的區網 IP：
```
http://192.168.x.x:8080/game.html
```
（在終端機輸入 `ipconfig`(Windows) 或 `ifconfig`(Mac) 查詢 IP）

---

## ☁️ Firebase Hosting 部署（之後用）

```bash
npm install -g firebase-tools
firebase login
firebase init hosting
# public 目錄輸入 . (當前目錄)
firebase deploy
```
部署後會得到 https://你的專案.web.app 的公開網址。

---

## 🔒 Realtime Database 安全規則（測試完成後替換）

在 Firebase Console → Realtime Database → 規則，貼上：

```json
{
  "rules": {
    "game": {
      "state":        { ".read": true,  ".write": true },
      "questions":    { ".read": true,  ".write": true },
      "canvas":       { ".read": true,  ".write": true },
      "aiImage":      { ".read": true,  ".write": true },
      "participants": { ".read": true,  ".write": true },
      "answers":      { ".read": true,  ".write": true }
    }
  }
}
```

---

## 🎮 操作流程

1. 開啟 `admin.html`，設定題目
2. 開啟 `show.html` 投影到大螢幕
3. 參賽者用手機開啟 `game.html` 加入
4. 管理員按「▶ 開始遊戲」
5. 每題：繪圖 → 可選 AI 增強 → 「🔓 開放答題」→ 「📊 公布結果」→「⏭ 下一題」
# spring_party_game2
