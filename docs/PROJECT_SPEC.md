# OpenThesis Project Specification

Status: Draft v0.1  
Date: 2026-07-29  
Working title: OpenThesis

## 1. Purpose

OpenThesis is an open-source, AI-centered company research system for serious individual long-term investors.

The product does not attempt to predict short-term stock-price movements. It helps a user answer:

1. How does this company make money?
2. What does its financial history reveal about business quality?
3. Which company- or industry-level growth opportunities may matter over the next three to five years?
4. What assumptions are already implied by the current valuation?
5. Which facts support or contradict the investment thesis?
6. What future evidence would strengthen, weaken, or invalidate the thesis?

The user owns the model choice and investment decision. OpenThesis owns the research protocol, evidence handling, deterministic calculations, prompt packs, agent orchestration, verification, and reproducibility.

## 2. Confirmed product decisions

### 2.1 Target user

The first target user is a serious individual investor who understands basic financial concepts but does not have access to an institutional research terminal or analyst team.

The first version is not optimized for:

- complete beginners who want a single buy/sell answer;
- institutional teams requiring proprietary datasets and collaboration;
- traders focused on intraday or short-term price movements.

### 2.2 Initial market

The first supported market is US-listed equities.

The first document source is SEC EDGAR:

- 10-K filings;
- filing metadata and history;
- Inline XBRL and extracted company facts.

Useful official references:

- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- https://www.sec.gov/data-research/structured-data/inline-xbrl

A-share and Hong Kong support are later market adapters. They must not delay the first complete US-company research workflow.

### 2.3 No transaction functionality

OpenThesis will not:

- connect to brokerage accounts;
- hold brokerage credentials;
- submit, modify, or cancel orders;
- perform automatic trading;
- generate short-term trading signals;
- optimize engagement by encouraging trading frequency;
- claim guaranteed or model-proven returns.

Watchlists, hypothetical portfolios, research alerts, and investment-thesis monitoring are allowed because they support research rather than execution.

### 2.4 Model choice belongs to the user

The product must support both local and cloud models through a provider abstraction.

Initial provider scope:

- Ollama or an equivalent local provider;
- OpenAI-compatible HTTP APIs;
- at least one native cloud-provider adapter where the generic protocol is insufficient.

Every report records the provider, model identifier, model parameters, workflow version, research-pack version, and data snapshot.

## 3. Product principles

1. **Bring your own model.**
2. **Bring your own permitted data.**
3. **Every factual claim needs traceable evidence.**
4. **Financial math is deterministic code, not language-model arithmetic.**
5. **Facts, inferences, assumptions, and unknowns are separate data types.**
6. **Forecasts are scenarios, not precise prophecies.**
7. **Every report is reproducible.**
8. **The system must be able to say that evidence is insufficient.**
9. **AI supports decisions; the user makes them.**
10. **No research module may disable core safety and verification rules.**

## 4. Core user journey

### 4.1 Configure a model

The user selects a local, cloud, or custom model endpoint. The system tests:

- connectivity;
- structured-output support;
- tool-calling support;
- context capacity;
- optional vision support;
- expected report cost and latency.

API keys stay in local secret storage and must not be saved in reports, logs, exported research packs, or the main application database in plaintext.

### 4.2 Select a company

The user searches by company name, ticker, or CIK. The system shows:

- available filings;
- fiscal periods;
- filing and amendment dates;
- structured financial facts;
- any missing or conflicting data.

### 4.3 Select a research workflow

Initial workflow choices:

- quick company overview;
- complete long-term fundamental research;
- financial-statement quality review;
- compare two filing periods;
- management promise tracking;
- growth-opportunity research;
- long-term scenario forecast;
- reverse DCF;
- challenge an existing investment thesis.

The primary MVP path is complete long-term fundamental research.

### 4.4 Review the research plan

Before execution, the product shows:

- agents and steps that will run;
- selected models;
- tools each agent may use;
- estimated cost;
- maximum runtime;
- maximum calls;
- installed research pack and version.

The user can start, cancel, pause, or rerun an individual failed step.

### 4.5 Inspect results

The result is a structured report rather than a single block of generated prose. A user can click any factual claim to open its exact source.

### 4.6 Save a thesis

The user can accept, edit, reject, or mark conclusions as unresolved. Saved theses preserve their assumptions, evidence, counterevidence, risks, and invalidation conditions.

## 5. MVP scope

### 5.1 Required MVP capabilities

The first usable release must:

1. identify a US-listed company;
2. download and cache its latest five annual 10-K filings;
3. parse filing structure, text, tables, and available XBRL facts;
4. normalize core financial facts across periods;
5. support at least one local and one cloud-compatible model path;
6. run specialized financial, business, risk, growth, forecast, and verification steps;
7. produce a structured report with clickable evidence;
8. distinguish facts, inferences, assumptions, and unknowns;
9. calculate financial metrics and valuation outputs deterministically;
10. save a versioned investment thesis;
11. compare outputs from two selected models;
12. import an `.othesis` research module;
13. run locally through a documented, low-friction installation path.

### 5.2 Explicitly deferred

- real-time market data;
- automated periodic monitoring;
- 10-Q and earnings-call coverage beyond what is needed for the first workflow;
- A-share and Hong Kong filing adapters;
- brokerage integration;
- transaction execution;
- technical-analysis indicators;
- social stock recommendations;
- multi-user cloud collaboration;
- a public model leaderboard;
- arbitrary executable-code plugins;
- mobile applications.

## 6. High-level architecture

```mermaid
flowchart TD
    SEC["SEC EDGAR and Inline XBRL"] --> INGEST["Filing ingestion"]
    INGEST --> DOC["Structured document store"]
    INGEST --> FACTS["Normalized financial facts"]
    DOC --> EVIDENCE["Evidence store"]
    FACTS --> CALC["Deterministic calculation engine"]

    USER["User and research pack"] --> ORCH["Workflow orchestrator"]
    ORCH --> TOOLS["Research tool API"]
    TOOLS --> EVIDENCE
    TOOLS --> FACTS
    TOOLS --> CALC

    ORCH --> PROVIDERS["Model provider adapters"]
    PROVIDERS --> LOCAL["Local models"]
    PROVIDERS --> CLOUD["Cloud models"]

    LOCAL --> VERIFY["Verification pipeline"]
    CLOUD --> VERIFY
    VERIFY --> ARTIFACTS["Versioned research artifacts"]
    ARTIFACTS --> REPORT["Report and thesis UI"]
```

### 6.1 Version 0.1 implementation shape

- Interface: native Python/Tkinter Windows desktop application.
- Domain, ingestion, orchestration, and reporting: typed Python modules.
- Initial database: local SQLite with schema migration support.
- Document storage: the user's local OpenThesis data directory.
- Background tasks: in-process worker threads with persisted run status and artifacts.
- Deployment: PyInstaller onedir bundle distributed as a portable Windows ZIP.
- External runtime dependencies: none for the frozen application.

The domain and provider boundaries remain intentionally independent from Tkinter so a
web interface, service API, or alternative desktop shell can be added later without
rewriting w~7æÚ$z{-®éÜj×æ÷B6†÷'B×FW&Ò7Fö6²F—&V7F–öã  ¢Ò&WfVçVRæB'W6–æW72ÖG&—fW"w&÷wFƒ°¢Ò÷W&F–ærÖ&v–ã°¢Òg&VR66‚fÆ÷s°¢Ò$ô”3°¢Ò6—FÂW‡VæF—GW&S°¢ÒF–ÇWF–öã°¢Ò6VÆV7FVB6ö×ç’×7V6–f–2µ—3°¢ÒFVf–æVB&—6²ÖWfVçB&ö&&–Æ—F–W2à ¢222Rã"f÷&V67B÷WGW@ ¦–ÖÀ§66Væ&–ó¢&6P¦†÷&—¦öå÷–V'3¢P§&ö&&–Æ—G“¢ãS §&WfVçVUö6w# ¢ö–çC¢ã¢&ævS¢³ã‚ÂãEÐ§FW&Ö–æÅö÷W&F–æuöÖ&v–ã ¢ö–çC¢ã#¢&ævS¢³ã‚Âã#5Ð¦77V×F–öç3¢µÐ¦–çfÆ–FF–öåö6öæF—F–öç3¢µÐ¦  ¤&V"Â&6RÂæB'VÆÂ&ö&&–Æ—F–W2×W7B&RW‡Æ–6—BæBæ÷&ÖÆ—¦VBà ¢222Rã2fÇVF–öà ¤–æ—F–ÂfÇVF–öâFööÇ3  ¢Ò&WfW'6RD4c°¢Òf÷'v&BD4c°¢Ò6Vç6—F—f—G’æÇ—6—3°¢Ò†—7F÷&–6ÂfÇVF–öâ6öçFW‡Bà ¥F†R6Æ7VÆF–öâVæv–æRW&f÷&×2ÆÂfÇVF–öâÖF‚âvVçG2W‡Æ–âæB6†ÆÆVævR–çWG2à ¥F†R&VfW'&VBf—'7BVW7F–öâ—3  £âv†B÷W&F–ær77V×F–öç2×W7B&RG'VRf÷"F†R7W'&VçBÖ&¶WB&–6RFò&R§W7F–f–VCð ¢22bâÖöGVÆ"&W6V&6‚6·0 ¢222bã6¶vRf÷&Ö@ ¤&W6V&6‚ÖöGVÆR—2F—7G&–'WFVB2öæRæ÷F†W6—6f–ÆRâ—B—2¤•Ö6ö×F–&ÆRFV6Æ&F—fR6¶vRà ¤W†×ÆS  ¦FW‡@¦6öÖ×Væ—G’ç62Öw&÷wF‚æ÷F†W6—0®)IÎ)H)HÖæ–fW7Bç–ÖÀ®)IÎ)H)Hv÷&¶fÆ÷rç–ÖÀ®)IÎ)H)H&ö×G2ð®)IÎ)H)H66†VÖ2ð®)IÎ)H)H'VÆW2ð®)IÎ)H)H'V'&–72ð®)IÎ)H)HW†×ÆW2ð®)IÎ)H)HFW7G2ð®)IN)H)H$TDÔRæÖ@¦  ¥F†R&ö¦V7B6†—2âöff–6–Â'V–ÇBÖ–â6³  ¦FW‡@¦öff–6–ÂæÆöær×FW&ÒÖgVæFÖVçFÇ0¦  ¢222bã"6¶vR&W7öç6–&–Æ—F–W0 ¤6²Ö’FVf–æS  ¢Ò&W6V&6‚VW7F–öç3°¢ÒvVçB&ö×G3°¢Òv÷&¶fÆ÷r7FW3°¢ÒWf–FVæ6RÆWfVÇ3°¢Òw&÷wF‚Ö÷÷'GVæ—G’6FVv÷&–W3°¢Ò–æGW7G'’×7V6–f–2ÖWG&–73°¢Ò66Væ&–òÖFÖ—76–öâ'VÆW3°¢Ò÷WGWB66†VÖ3°¢Ò&W÷'B6V7F–öç3°¢Ò&Væ6†Ö&²'V'&–72à ¤6²FV6Æ&W2&WV—&VBÖöFVÂ6&–Æ—F–W2æBÆÆ÷vVB&W6V&6‚FööÇ2â—BFöW2æ÷B&WV—&R'F–7VÆ"ÖöFVÂfVæF÷"à ¢222bã26¶vRÆ–W&–æp ¦FW‡@¦æöâÖ÷fW'&–F&ÆR6÷&R6fWG’öÆ–7¢(i"öff–6–Â&6R&W6V&6‚6°¢(i"÷F–öæÂ6V7F÷"6°¢(i"W6W"÷fW'&–FRÆ–W ¦  ¢222bãB6V7W&—G’&W7G&–7F–öç0 ¥F†Rf—'7B6¶vRfW'6–öâÆÆ÷w3  ¢Ò”ÔÃ°¢ÒÖ&¶F÷vã°¢Ò¥4ôâ66†VÖ°¢Ò&W7G&–7FVBf÷&×VÆW‡&W76–öç2à ¤—BFöW2æ÷BÆÆ÷s  ¢Ò—F†öâÂ¦f67&—BÂ6†VÆÂÂ÷"&&—G&'’W†V7WF&ÆR6öFS°¢ÒVç&W7G&–7FVBæWGv÷&²&WVW7G3°¢Òf–ÆW7—7FVÒ66W73°¢Ò6V7&WB66W73°¢ÒG&ç67F–öâ6&–Æ—F–W3°¢ÒÖöF–f–6F–öâöb&r6÷W&6RFö7VÖVçG2÷"öff–6–Âf7G2à ¥&W7G&–7FVBf÷&×VÆ2&R'6VB'’6fRW‡&W76–öâVæv–æRâF†W’&RæWfW"76VBFòWfÆà ¢222bãR–ç7FÆÆF–öà ¤&Vf÷&R–ç7FÆÆF–öâF†RÆ–6F–öã  £âfÆ–FFW2F†R&6†—fRæBÖæ–fW7C°£"â6†V6·26ö×F–&–Æ—G“°£2âF—7Æ—2&WVW7FVBFööÇ2æBW&Ö—76–öç3°£Bâ66ç2f÷"&ö†–&—FVBf–ÆW2æB6öç7G'V7G3°£Râ'Vç2–æ6ÇVFVBfÆ–FF–öâFW7G3°£bâ&V6÷&G26¶vR–BÂfW'6–öâÂ6÷W&6RÂæB6öçFVçB†6‚à ¤'V–ÇBÖ–âÂ6–væVBÖ6öÖ×Væ—G’ÂÆö6Â×W6W"ÂæBVçG'W7FVB6¶vW2&Rf—6–&Ç’F—7F–æwV—6†VBà ¢22râ&W&öGV6–&–Æ—G ¤WfW'’&W÷'B&V6÷&G3  ¦–ÖÀ¦vVæW&FVEöC¢##bÓrÓ#•C#£3£ ¦FFö5ööc¢##bÓrÓ#•C££ §6÷W&6U÷6æ6†÷Eö†6ƒ¢6†#Sc¢ââà§&÷f–FW#¢W6W"×6VÆV7FV@¦ÖöFVÃ¢&÷f–FW"ÖÖöFVÂÖ–@¦ÖöFVÅ÷&ÖWFW'3 ¢FV×W&GW&S¢ã §&W6V&6…÷6³ ¢–C¢öff–6–ÂæÆöær×FW&ÒÖgVæFÖVçFÇ0¢fW'6–öã¢ãã ¢†6ƒ¢6†#Sc¢ââà§v÷&¶fÆ÷s ¢–C¢6ö×ÆWFRÖgVæFÖVçFÂ×&W6V&6€¢fW'6–öã¢ãã ¦Æ–6F–öå÷fW'6–öã¢ãã ¦  ¥W6W"VF—G2&R7F÷&VB2æWrfW'6–öç2æBGG&–'WFVBFòF†RW6W"&F†W"F†â&V–ær&W6VçFVB2ÖöFVÂ÷WGWBà ¢22‚â&Væ6†Ö&²FW6–và ¥F†R&Væ6†Ö&²ÖV7W&W2f–ææ6–Â&W6V&6‚&–Æ—G’6W&FVÇ’g&öÒgWGW&R–çfW7FÖVçBW&f÷&Öæ6Rà ¢222‚ãf–Æ–ær&W6V&6‚G&6° ¤–æ—F–Â66÷&–æs  §ÂF–ÖVç6–öâÂvV–v‡BÀ§ÂÒÒ×ÂÒÒÓ§À§Âf–ææ6–Âf7B67W&7’Â#RÀ§Â6Æ7VÆF–öâ67W&7’ÂRRÀ§Â6—FF–öâ6÷'&V7FæW72æB6ö×ÆWFVæW72Â#RÀ§ÂV&æ–æw2×VÆ—G’æB&—6²F—66÷fW'’ÂRRÀ§Âf7Bö–æfW&Væ6R÷Væ¶æ÷vâ6W&F–öâÂRÀ§Â6÷VçFW&&wVÖVçBVÆ—G’ÂRÀ§Â6öç6—7FVæ7’æB&W&öGV6–&–Æ—G’ÂRRÀ§Â6÷7BæBÆFVæ7’ÂRRÀ ¤æVvF—fRÖWG&–72&R&W÷'FVB6W&FVÇ“  ¢ÒVç7W÷'FVB6Æ–Ò&FS°¢Ò6—FF–öâW'&÷"&FS°¢ÒçVÖW&–2†ÆÇV6–æF–öâ&FS°¢Ò÷fW&6öæf–FVæ6R&FRà ¢222‚ã"çF’Ö6öçFÖ–æF–öâ7G&FVw ¢Òg&WVVçFÇ’&Vg&W6†VB÷7BÖ7WFöfbf–Æ–æw3°¢Ò†–FFVâ&—fFRWfÇVF–öâ6WG3°¢Òæöç–Ö—¦VB6ö×æ–W3°¢Ò–çFW&æÆÇ’6öç6—7FVçBG&ç6f÷&ÖVBf–ææ6–Â7FFVÖVçG3°¢Ò6öçG&öÆÆVBFööÇ2v—F‚æWGv÷&²66W72F—6&ÆVC°¢ÒWf–FVæ6R×&WV—&VB66÷&–æs°¢ÒV&Æ–2FWfVÆ÷ÖVçB66W26W&FVBg&öÒÆVFW&&ö&B66W2à ¢222‚ã2Æ—fR&÷7V7F—fRG&6° ¥F†RÖöFVÂ7V&Ö—G2g&÷¦VâÂF–ÖW7F×VB&ö&&–Æ—G’F—7G&–'WF–öç2f÷"gWGW&R'W6–æW72f&–&ÆW2æB&—6²WfVçG2â÷WF6öÖW2&R66÷&VBÆFW"W6–æs  ¢Ò–çFW'fÂ6÷fW&vS°¢Òf÷&V67BW'&÷#°¢Ò'&–W"66÷&R÷"æ÷F†W"&÷W"66÷&–ær'VÆS°¢Ò&ö&&–Æ—G’6Æ–'&F–öã°¢Ò&—6²ÖWfVçB&V6—6–öâæB&V6ÆÃ°¢ÒF†W6—2Ö'&V²FWFV7F–öâFVÆ’à ¥7Fö6²&WGW&âÖ’&R6†÷vâ26V6öæF'’6öçFW‡BÂæ÷BW6VB2F†R&–Ö'’ÖV7W&Röbf–Æ–ær×&W6V&6‚VÆ—G’à ¢222‚ãBWfÇVF–öâv÷fW&ææ6P ¤÷VâÖVæFVB66W2W6RW‡W'BÖWF†÷&VBFöÖ–2'V'&–72âæ÷F†W"ÆæwVvRÖöFVÂ×W7Bæ÷B&RF†R6öÆR§VFvRà ¢22’âf–ÇW&R&V†f–÷  ¥F†RÆ–6F–öâ×W7Bf–Âf—6–&Ç’æB6fVÇ’à ¤v÷&¶fÆ÷r7FWÖ’&WGW&ã  ¦–ÖÀ§7FGW3¢–ç7Vff–6–VçEöWf–FVæ6P§&V6öã ¢Ò6VvÖVçBFFVæf–Æ&ÆP¢ÒGvòöff–6–Âf7G26÷VÆBæ÷B&R&V6öæ6–ÆV@§&V6öÖÖVæFVEö7F–öã ¢Ò–ç7V7Bf–Æ–ær6V7F–öâÖçVÆÇ¢Ò&÷f–FRâFF—F–öæÂ6÷W&6P¦  ¥F†RÆ–6F–öâ&Æö6·2f–æÂV&Æ–6F–öâv†Vã  ¢Ò7&—F–6ÂçVÖW&–6ÂfÆ–FF–öâf–Ç3°¢Ò6—FF–öâFöW2æ÷BW†—7C°¢Ò&W÷'F–ærW&–öG2÷"66÷W2&RÖ—†VC°¢Ò&WV—&VBf÷&V67B–çWG2&RÖ—76–æs°¢Ò6÷W&6RfW'6–öç26öæfÆ–7C°¢Ò7G'V7GW&VBÖöFVÂ÷WGWB&VÖ–ç2–çfÆ–BgFW"&÷VæFVB&WG&–W2à ¢22#â6V7W&—G’æB&—f7 ¢222#ãVçG'W7FVB–çWG0 ¤f–Æ–æw2ÂvV'vW2ÂW6W"Fö7VÖVçG2Â&ö×G2ÂæB&W6V&6‚6·2&RVçG'W7FVB–çWBà ¥F†R7—7FVÒ×W7BFVfVæBv–ç7C  ¢Ò&ö×B–æ¦V7F–öâ–âFö7VÖVçG3°¢ÒÖÆ–6–÷W2Dg2÷"&6†—fW3°¢ÒF‚G&fW'6Â–âæ÷F†W6—66¶vW3°¢Ò6V7&WBW‡G&7F–öã°¢ÒVæWF†÷&—¦VBæWGv÷&²66W73°¢Ò6W'fW"×6–FR&WVW7Bf÷&vW'’F‡&÷Vv‚7W7FöÒVæGö–çG3°¢ÒVç6fRf÷&×VÆWfÇVF–öã°¢ÒÆöw26öçF–æ–ær7&VFVçF–Ç2÷"&—fFR&W6V&6‚à ¢222#ã"ÖöFVÂFFF—66Æ÷7W&P ¤&Vf÷&R6Æ÷VBÖÖöFVÂ6ÆÂÂF†RW6W"6â6VRv†–6‚Fö7VÖVçG2÷"W†6W'G2v–ÆÂÆVfRF†RÆö6Â7—7FVÒà ¥&÷f–FW"×7V6–f–2FF×&WFVçF–öâ&V†f–÷"6†÷VÆB&RFö7VÖVçFVBv†W&R¶æ÷vââÆö6ÂÖöæÇ’ÖöFR×W7Bæ÷B6–ÆVçFÇ’6ÆÂ6Æ÷VB6W'f–6W2à ¢222#ã2FööÂW&Ö—76–öç0 ¤WfW'’vVçBæB&W6V&6‚6²&V6V—fW2âW‡Æ–6—BFööÂÆÆ÷vÆ—7BâFööÂ6ÆÇ2Â&wVÖVçG2Â÷WGWG2ÂæBfÆ–FF–öâ&W7VÇG2&R&V6÷&FVB–âF†R&W6V&6‚'Vâà ¢22#â6÷7BæBW&f÷&Öæ6R6öçG&öÇ0 ¥F†R7—7FVÒ7W÷'G3  ¢Ò'VâÖÆWfVÂ6÷7BÆ–Ö—G3°¢Ò6ÆÂÖ6÷VçBÆ–Ö—G3°¢ÒÖ†–×VÒ'VçF–ÖS°¢Ò&÷VæFVB&WG&–W3°¢Ò6æ6VÆÆF–öã°¢Ò7FWÖÆWfVÂ&W'Vã°¢Ò'6VBÖFö7VÖVçB66†–æs°¢Òf–ææ6–ÂÖf7B66†–æs°¢Ò&ö×BæB&W7öç6R66†–ærv†W&R6fS°¢Ò–æ7&VÖVçFÂæÇ—6—2öbæWrf–Æ–æw3°¢Ò6ÖÆÆW"ÖÖöFVÂ&÷WF–ærf÷"W‡G&7F–öâF6·3°¢Ò7G&öævW"ÖÖöFVÂ&÷WF–ærf÷"7–çF†W6—2æB6†ÆÆVævRF6·2à ¤æWrf–Æ–ær6†÷VÆBWFFRffV7FVBF†W6—2æöFW2&F†W"F†âf÷&6R6ö×ÆWFR&VæÇ—6—2öbÆÂ†—7F÷'’à ¢22#"â÷Vâ×6÷W&6RW‡FVç6–öâÆ–÷W@ ¥&÷÷6VB&W÷6—F÷'’÷&væ—¦F–öã  ¦FW‡@¦2ð¢vV"ð§6W'f–6W2ð¢’ð§6¶vW2ð¢FöÖ–âð¢&÷f–FW"×6F²ð¢&W6V&6‚×6²×6F²ð¢FööÂ×&÷Fö6öÂð¢66†VÖ2ð§&÷f–FW'2ð¦Ö&¶WG2ð¢W2×6V2ð§&W6V&6‚×6·2ð¢öff–6–ÂæÆöær×FW&ÒÖgVæFÖVçFÇ2ð§v÷&¶fÆ÷w2ð¦&Væ6†Ö&·2ð¦Fö72ð¦  ¤6öçG&–'WF–öâVæ—G26†÷VÆB&VÖ–â6ÖÆÃ  ¢Ò&÷f–FW"FFW'3°¢ÒÖ&¶WBFFW'3°¢Ò&ö×G3°¢Ò&W6V&6‚6·3°¢Òf–ææ6–ÂÖWG&–73°¢Ò&Væ6†Ö&²66W3°¢ÒG&ç6ÆF–öç2à ¢22#2âÆ–6Vç6–æræB'W6–æW72ÖöFVÀ ¤Æ–6Vç6R—2æ÷B–WBFV6–FVBà ¤6æF–FFR&ö6†W3  ¢Ò6†RÓ"ãf÷"'&öBF÷F–öâæBV6÷7—7FVÒw&÷wFƒ°¢ÒuÂf÷"7G&öævW"&÷FV7F–öâv–ç7BVç6†&VB†÷7FVBf÷&·3°¢ÒGVÂÆ–6Vç6–ærf÷"â÷Vâ6öÖ×Væ—G’VF—F–öâæB6öÖÖW&6–ÂW6Rà ¥F†R÷Vâ×6÷W&6RfW'6–öâ6†÷VÆB&VÖ–âvVçV–æVÇ’W6&ÆS  ¢ÒÆö6ÂW†V7WF–öã°¢ÒW6W"×6VÆV7FVBÖöFVÇ3°¢Ò4T2f–Æ–ær–ævW7F–öã°¢ÒgVÆÂ6÷&R&W6V&6‚v÷&¶fÆ÷s°¢ÒF†W6—2ÖævVÖVçC°¢Ò&6–2ÖöFVÂ6ö×&—6öã°¢Ò&W6V&6‚×6²–×÷'Bà ¥÷FVçF–ÂgWGW&R†÷7FVB6W'f–6W2Ö’6†&vRf÷#  ¢ÒÖævVB–æg&7G'V7GW&S°¢ÒWFöÖF–2f–Æ–ærWFFW3°¢Ò×VÇF’ÖFWf–6R7–æ6‡&öæ—¦F–öã°¢ÒFVÒ6öÆÆ&÷&F–öã°¢ÒW&Ö—GFVB6öÖÖW&6–ÂFF°¢ÒÆ&vR×66ÆR67&VVæ–æs°¢Òæ÷F–f–6F–öâFVÆ—fW'’à ¢22#Bâ7V66W727&—FW&– ¤Õe7V66W72—2æ÷BÖV7W&VB'’–çfW7FÖVçB&WGW&ç2à ¤–æ—F–Â&öGV7BÖWG&–73  ¢ÒF–ÖRg&öÒF–6¶W"VçG'’Fò6—FVB&W6V&6‚&W÷'C°¢Òf7GVÂæB6—FF–öâ67W&7“°¢ÒVç7W÷'FVBÖ6Æ–Ò&FS°¢ÒW&6VçFvRöb6Æ–×2–ç7V7F&ÆRg&öÒ6÷W&6S°¢Ò7V66W76gVÂÆö6Â–ç7FÆÆF–öâ&FS°¢Ò7V66W76gVÂ6ö×ÆWF–öâ&FR7&÷727W÷'FVBÖöFVÇ3°¢Ò6÷7BæBÆFVæ7’W"v÷&¶fÆ÷s°¢ÒçVÖ&W"æBVÆ—G’öbW‡FW&æÂ&W6V&6‚×6²÷&÷f–FW"6öçG&–'WF–öç3°¢Òv†WF†W"W6W'2&WGW&âFòWFFRâW†—7F–ærF†W6—2&F†W"F†âöæÇ’vVæW&FRöæRÖöfb&W÷'G2à ¢22#Râ–×ÆVÖVçFF–öâÖ–ÆW7FöæW0 ¢222Ö–ÆW7FöæR¢6öçG&7G2æBf—‡GW&W0 ¢Òf–æÆ—¦R&ö¦V7BæÖR÷"&W6W'fRv÷&¶–ærF—FÆS°¢Ò6†ö÷6RÆ–6Vç6S°¢ÒFVf–æRFöÖ–â66†VÖ3°¢Ò7&VFR6ÖÆÂg&÷¦Vâ4T2f–Æ–ærf—‡GW&S°¢ÒFVf–æRWf–FVæ6RæB6Æ–Ò6öçG&7G3°¢ÒFVf–æR&÷f–FW"æBFööÂ–çFW&f6W3°¢ÒFVf–æRæ÷F†W6—6cÖæ–fW7BæBfÆ–FF–öâ'VÆW2à ¤W†—B7&—FW&–öã¢6öçG&7G26â&W&W6VçBöæR6ö×ç’w2f–Æ–ærÂ6Æ–×2ÂWf–FVæ6RÂv÷&¶fÆ÷rÂæB&W6V&6‚6²v—F†÷WB'Vææ–ærÖöFVÂà ¢222Ö–ÆW7FöæR¢FWFW&Ö–æ—7F–2f–Æ–ærf÷VæFF–öà ¢Ò4T26ö×ç’Æöö·WæBf–Æ–ærF÷væÆöC°¢Ò…DÔÂæB–æÆ–æR„%$Â'6–æs°¢Ò6æöæ–6Âf7G2æB6÷W&6Ræ6†÷'3°¢Ò6÷&Rf–ææ6–Â6Æ7VÆF–öç3°¢ÒÆö6ÂW'6—7FVæ6S°¢Òö–çBÖ–â×F–ÖRÖWFFFà ¤W†—B7&—FW&–öã¢F†RÆ–6F–öâ&öGV6W2â67W&FRÂ6—FVBFWFW&Ö–æ—7F–2f–ææ6–Â7VÖÖ'’v—F†÷WB’à ¢222Ö–ÆW7FöæR#¢6–ævÆRÖÖöFVÂ&W6V&6€ ¢Ò&÷f–FW"'7G&7F–öã°¢ÒÆö6ÂæB6Æ÷VBÖ6ö×F–&ÆRFFW'3°¢Ò&ö×B6ö×–ÆW#°¢Ò6öçG&öÆÆVB&W6V&6‚FööÇ3°¢Ò7G'V7GW&VBf–ææ6–ÂæB'W6–æW72&W6V&6ƒ°¢Ò6—FF–öâæBçVÖW&–6ÂfW&–f–6F–öâà ¤W†—B7&—FW&–öã¢öæR6VÆV7FVBÖöFVÂ&öGV6W2&W÷'Bv†÷6Rf7GVÂ6Æ–×26â&R–ç7V7FVBæBfW&–f–VBà ¢222Ö–ÆW7FöæR3¢×VÇF’ÖvVçB—VÆ–æP ¢Ò&ÆÆVÂ&W6V&6‚&öÆW3°¢ÒfW&–f–VB&W6V&6‚F÷76–W#°¢Òw&÷wF‚æB6¶WF–6ÂvVçG3°¢ÒÆöær×FW&Ò66Væ&–òvVçC°¢ÒFWFW&Ö–æ—7F–2fÇVF–öã°¢Ò7–çF†W6—2v—F‚Vç&W6öÇfVBF—6w&VVÖVçG2à ¤W†—B7&—FW&–öã¢6ö×ÆWFRVæB×FòÖVæB&W÷'B6â&R&W'Vâg&öÒ&V6÷&FVB–çWG2à ¢222Ö–ÆW7FöæRC¢F†W6—2æBÖöGVÆRV6÷7—7FVÐ ¢ÒF†W6—2w&‚æB†—7F÷'“°¢Òæ÷F†W6—6–×÷'BÂW&Ö—76–öç2ÂfÆ–FF–öâÂæBFW7G3°¢Òöff–6–Â'V–ÇBÖ–â&W6V&6‚6³°¢ÒÖöFVÂ6ö×&—6öã°¢ÒW‡÷'F&ÆR&W÷'Bà ¤W†—B7&—FW&–öã¢W6W"6â–ç7FÆÂ6fRFV6Æ&F—fRÖöGVÆRæBW6R—B–â&W&öGV6–&ÆR&W6V&6‚'Vâà ¢222Ö–ÆW7FöæRS¢&Væ6†Ö&° ¢ÒV&Æ–2FWfVÆ÷ÖVçB6WC°¢Ò†–FFVâWfÇVF–öâ'VææW#°¢Òö&¦V7F—fRf–Æ–ærÖWG&–73°¢Ò&Væ6†Ö&²&W÷'Bf÷&ÖC°¢Òf÷VæFF–öâf÷"gWGW&RÆ—fR&÷7V7F—fR7V&Ö—76–öç2à ¢22#bâfW'6–öâãFV6—6–öç0 ¥F†Rf—'7B&VÆV6Rf—†W2F†RföÆÆ÷v–ær6†ö–6W3  £â&ö¦V7BæB6¶vRæÖS¢÷VåF†W6—3°£"âÆ–6Vç6S¢6†RÓ"ã°£2â–çFW&f6S¢æF—fRF¶–çFW"FW6·F÷6†VÆÃ°£Bâ6¶v–æs¢÷'F&ÆRv–æF÷w2ƒcBÆ–6F–öã°£Râ&÷f–FW"F‡3¢öÆÆÖæB÷Vä’Ö6ö×F–&ÆR6†BÖ6ö×ÆWF–öç2—3°£bâæ÷&ÖÆ—¦VBf7G3¢&WfVçVRÂ÷W&F–ær–æ6öÖRÂæWB–æ6öÖRÂ÷W&F–ær66‚fÆ÷rÀ¢6—FÂW‡VæF—GW&RÂ76WG2ÂÆ–&–Æ—F–W2ÂWV—G’Â66‚Â&V6V—f&ÆW2Â–çfVçF÷'’À¢æB6†&W2÷WG7FæF–æs°£râ6÷W&6R66÷S¢4T2Ô²f–Æ–ær…DÔÂæB6ö×ç’f7G2„%$Ã°£‚âÖöGVÆW3¢FV6Æ&F—fRÂW&Ö—76–öâÖÆ–Ö—FVBæ÷F†W6—66¶vW3°£’â6V7&WG3¢6W76–öâÖöæÇ’æBæWfW"7F÷&VB'’÷VåF†W6—2à ¥7F–ÆÂ÷Vâf÷"ÆFW"&6†—FV7GW&RFV6—6–öç3  ¢ÒFF—F–öæÂ–æGW7G'’FF6÷W&6W3°¢ÒÕæBV&æ–æw2Ö6ÆÂ–ævW7F–öã°¢ÒW'6—7FVçBF6²6æ6VÆÆF–öâæB&WG'“°¢ÒFVfVÇBÖöFVÂ6÷7B'VFvWG2æB&÷WF–æs°¢Ò6–væVB6öÖ×Væ—G’F—7G&–'WF–öâf÷"æ÷F†W6—66¶vW3°¢ÒvV"÷"7&÷72×ÆFf÷&Ò–çFW&f6Rà ¢22#râfW'6–öâã–×ÆVÖVçFF–öâ7FGW0 ¥F†Rf—'7BW6&ÆRfW'F–6Â6Æ–6R—2–×ÆVÖVçFVC  ¦FW‡@¥F–6¶W"÷"6ö×ç’æÖP®(i"4T2f–Æ–æræB„%$Â–ævW7F–öà®(i"7G'V7GW&VBFW‡BÂF&ÆRÂæBæ÷&ÖÆ—¦VBf7BWf–FVæ6P®(i"FWFW&Ö–æ—7F–2ÖWG&–70®(i"÷F–öæÂ&WfW'6RD4b–×Æ–VBW‡V7FF–öç0®(i"6öæf–wW&&ÆR×VÇF’ÖvVçB&W6V&6€®(i"Wf–FVæ6RfW&–f–6F–öâæB÷F–öæÂÖöFVÂ6ö×&—6öà®(i"&W÷'BæBVæBÖöæÇ’–çfW7FÖVçBF†W6—0®(i"÷'F&ÆRv–æF÷w2Æ–6F–öà¦  ¥&VÆV6R66WFæ6R—2W†W&6—6VB'’F†RWFöÖFVBVæ—B7V—FRÂâöffÆ–æRFWFW&Ö–æ—7F–0§v÷&¶fÆ÷r6Öö¶RFW7BÂæBÖVB×v–æF÷ruT’6Öö¶RFW7Bv–ç7BF†Rg&÷¦VâW†V7WF&ÆRà¥F†R&Væ6†Ö&²Ö–ÆW7FöæR&VÖ–ç2–çFVçF–öæÆÇ’FVfW'&VC¢–çfW7FÖVçB×&WGW&â&6·FW7G2&P¦æ÷BG&VFVB2fÆ–B6†÷'F7WBf÷"ÖV7W&–ærWf–FVæ6RVÆ—G’÷"&W6V&6‚F—66—Æ–æRà 