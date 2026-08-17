<div align="center">

# OpenThesis

**以 AI 為核心、以證據為基礎的長期公司研究工具**

[English](README.md) · [簡體中文](README.zh-CN.md) · [繁體中文](README.zh-Hant.md)

[![Release](https://img.shields.io/github/v/release/zjy1346/OpenThesis?display_name=tag&sort=semver)](https://github.com/zjy1346/OpenThesis/releases/latest)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)](https://github.com/zjy1346/OpenThesis/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

研究公司，而不是預測短期價格。

</div>

OpenThesis 是面向個人長期投資者的開源桌面研究系統。它將公開財報、確定性財務分析與專業 AI Agent 組合為可追溯的投資論點。模型由您選擇；OpenThesis 提供研究流程、證據協定、財務工具與可重現性。

> [!IMPORTANT]
> OpenThesis 不連線券商帳戶、不執行交易、不提供短線訊號，也不承諾投資回報。

## 主要功能

- **自行選擇模型。** 支援 DeepSeek、Qwen、Kimi、GLM、OpenAI、Gemini、OpenRouter、Ollama，以及任意 OpenAI-compatible 端點。
- **先證據，後觀點。** AI 產生的事實性結論必須引用本次研究收集的財報證據。
- **確定性財務計算。** 財務概覽與反向 DCF 由程式計算，不交給語言模型自由編造。
- **專業 Agent 協作。** 財務、商業模式、會計風險、增長、反方審查、預測、綜合與驗證 Agent 共用同一組證據。
- **研究可重現。** 每次執行會記錄模型、參數、研究模組、資料快照與報告語言。
- **本地優先與隱私。** API Key、視覺 Token 與自訂端點密鑰只存在目前工作階段，不寫入資料庫或報告。

## 研究流程

1. 選擇上市市場與公司，確認代碼、交易所及官方披露來源。
2. 選擇研究模組與報告語言；介面語言可跟隨系統或手動選擇。
3. 先由結構化資料與官方 PDF 解析財報，按期間、合併口徑、幣種、單位與證據頁驗證。
4. 通過品質門的事實才會送入 Agent；不足或矛盾的資料會隔離，不會被模型補造。
5. 閱讀確定性財務概覽、研究報告與技術詳情，必要時匯出 Markdown 或 HTML。

## 語言設定

OpenThesis 內建 `zh-CN`、`zh-Hant` 與 `en`。新安裝預設跟隨作業系統語言；設定中可改為手動選擇。介面語言與報告語言彼此獨立，例如使用繁體介面並輸出英文報告。`zh-TW`、`zh-HK`、`zh-Hant` 等外部標籤會統一為 `zh-Hant`。

## 安裝與安全

Windows 測試版可從 Releases 下載 portable ZIP，解壓後執行 `OpenThesis\OpenThesis.exe`。首次啟動不呼叫 AI；只有您主動選擇模型並開始研究時，研究上下文才會送往您設定的服務。API Key 不會寫入本機設定、研究歷史或日誌。

視覺財報備援是可選的雲端功能，僅在本地解析失敗、您明確同意後上傳必要財務表頁，並受 20 頁／10 MB 限制。OpenThesis 不訓練、不下載、不捆綁本地模型。

## 研究模組

內建 `official.long-term-fundamentals` 模組涵蓋財務、商業模式、會計風險、增長機會、反方審查與長期情境。`.othesis` 模組是受權限限制的宣告式 ZIP，不可要求 network、filesystem 或 execute_code 權限。

## 支援的市場與來源

OpenThesis 將上市幣種與財報報告幣種分開保存。支援美股 SEC EDGAR、港股 HKEX／發行人披露，以及滬深北交易所與巨潮資訊官方披露。研究會先選擇最近且適用的年報、季報或中期報告，並保留公告識別、報告期、修訂關係、頁碼與原文證據。

財報核心事實包括收入、淨利潤、經營現金流、資產、負債與權益；可用時也會計算營業利潤率、自由現金流、資本支出及年度連續性。不同期間、合併口徑、幣種或單位不會被混合；失敗的年度會顯示為不可用，而不是靜默當成零。

## 模型與資料邊界

你可以使用內建模型目錄、手動模型 ID、OpenAI-compatible 端點或本地 Ollama。模型只接收通過品質門的事實與必要研究上下文；確定性計算由本地程式完成。模型輸出會經過協定白名單與語言投影，避免將內部 JSON 欄位直接顯示給一般讀者。

雲端視覺財報備援不是預設功能。只有本地結構化來源及 PDF 表格解析失敗、你開啟功能並勾選上傳同意後，系統才會定位缺失的合併財務表頁。上傳前會顯示文件、頁面、大小與指紋供確認；每個視覺候選仍須通過相同的期間、單位、幣種、口徑與勾稽品質門。

## 隱私與安全檢查

- API Key、SEC 聯絡信箱、MinerU Token 與自訂視覺 API Key 僅保留在目前工作階段。
- 研究歷史與設定不保存上述秘密；錯誤診斷只保留安全的錯誤類型與階段資訊。
- 發布壓縮檔不包含使用者資料庫、研究歷史、個人絕對路徑或開發憑證。
- `.othesis` 研究模組會先驗證權限宣告；不允許要求執行程式碼、任意檔案系統或網路存取。

## 匯出與審計

報告可匯出為 Markdown 或 HTML。一般模式顯示本地化的執行摘要、主要結論、反方觀點、失效條件、領先指標與未解決問題；技術模式另外顯示來源、證據頁、品質驗證與隔離原因。證據 ID 與協定鍵保持穩定，便於在本機重現研究，不代表投資建議。

## 開發與測試詳情

Python 核心需要 Python 3.11 或更新版本；桌面工作區使用 Node.js 與 pnpm。常用檢查如下：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -p "test_*.py"
cd desktop
pnpm test
pnpm build
```

修改語言目錄時，請保持 `language-contract.json`、Python 語言註冊表與 TypeScript 註冊表的 ID、別名、HTML 語言標籤、文字方向及 fallback 一致。新增語言應先加入註冊表與完整目錄，再新增測試，不要在頁面元件內散落語言分支。

## 版本與回饋

請在回報問題時附上 OpenThesis 版本、作業系統、研究市場、報告期及不含秘密的錯誤階段。不要貼 API Key、SEC 聯絡信箱、研究資料庫或完整財報 PDF；如需重現，請提供官方公告識別與最小必要頁面資訊。

## 常見研究情境

### 年報、季報與中期報告

系統會將 FY、Q1、H1／Q2 與 Q3／9M 分開選取，季度或中期數字只和相同期間比較。公告日期不會被誤當成報告期末；非自然財年的起始日也會依官方期間推導。更正公告和修訂報告會依公告類別、修訂關係與權威時間排序。

### 合併與母公司口徑

財務表會優先辨識正式的合併損益表、資產負債表與現金流量表。母公司、單體或附註中的相似欄位不會與合併欄位混用。每個事實保留表名、列標籤、期間欄位、頁碼、原文摘錄與來源指紋，方便回到官方文件核對。

### 失敗與隔離

如果資產不等於負債加權益、單位或幣種不一致、核心欄位不足、來源證據不完整，該期間會進入隔離區。隔離事實仍可在技術詳情中審計，但不會進入確定性指標、研究上下文或模型提示。缺失值顯示為「—」，不會被當作零。

## 研究包權限

研究包以 ZIP 形式保存 manifest、工作流與提示模板。安裝前會驗證 API 版本、雜湊、支援語言及權限聲明；只接受 Markdown、JSON-compatible YAML、JSON Schema 與文字內容。研究包不能要求執行程式碼、任意檔案系統、網路或秘密存取。

自訂研究包可以加入領域問題、比較維度與輸出段落，但 OpenThesis 仍會追加證據要求、財務品質門、報告語言約束及安全白名單。內部協定鍵和 enum 不因翻譯而改變。

## 使用建議

首次研究建議先選擇離線合成示範公司，確認報告語言、介面語言與匯出格式，再設定模型。真實美股研究前先填寫自己的 SEC 請求者身份與可聯絡電郵；這不是目標公司的投資者關係電郵，也不會被 OpenThesis 代替填寫。

研究前請確認官方披露來源、報告期與幣種。對跨市場公司，上市幣種可能是 HKD，而財報報告幣種可能是 CNY、USD 或其他披露幣種；報告會分別顯示，避免讀者誤以為程式自動換匯。

研究完成後可先閱讀確定性財務概覽，再閱讀主要結論、反方觀點和未解決問題。低置信度內容會以不同視覺標記顯示；關閉技術詳情後，畫面不會顯示證據 ID、內部欄位或原始協定錯誤。

## 開源與授權

OpenThesis 使用 Apache-2.0 授權。歡迎提交不含秘密與個人資料的問題回報、測試案例或文件改善；涉及官方財報時，請引用公告 ID、來源 URL、報告期與最小必要摘錄，不要提交完整受版權限制的報告。

## 開發

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -p "test_*.py"
cd desktop
pnpm test
pnpm build
```

提交前請執行 `git diff --check`，並確認測試輸出與打包檔案沒有 API Key、SEC 聯絡信箱、使用者資料庫或個人路徑。

## 授權

Apache-2.0。OpenThesis 是研究輔助工具，不構成投資建議。
