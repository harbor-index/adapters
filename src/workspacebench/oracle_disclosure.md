# Oracle disclosure

Tasks whose shipped oracle (`solution/`) writes a value a rubric requires although the task's inputs lack or contradict it.
On these tasks the oracle run checks the grading pipeline, not whether the task can be solved from its inputs.

Of the 723 shipped tasks: COPIED 109 (the oracle writes a rubric value the inputs lack or contradict), LABELLED 16 (the answer itself marks the value as an assumption), CLEAN 59 (checked; every value comes from the inputs), UNCLEAR 6. Sources: independent audits of the oracle answers, and the notes the oracle builders kept for each task (builder's notes are a lower bound for COPIED: tasks never flagged were not audited). Excluded tasks are listed in `excluded_tasks.json` instead.

| task | class | source | what |
|---|---|---|---|
| cn-009 | COPIED | builder's notes | rubric 7: Health score 56/100 ("亚健康") stated as fact in HTML; not in inputs |
| cn-011 | COPIED | independent audit | rubric-15 trend sentence copied from the rubric; same sentence is in the audit record |
| cn-015 | COPIED | builder's notes | rubric 8, 9: File-6 expense value 2500 (Q4 cost is 2300) inserted; 5th row (Nov 1400) is filler. Rubric 7 value 576 NOT copied (followed inputs 476) |
| cn-016 | COPIED | builder's notes | rubric 6, 8 and others: Per-dept hiring demand/completed, top-3 resignation reasons, session/participant counts (116 / 897) constructed; extra "uncategorised" training row of 17 to reach 116; extra 净流动率 column |
| cn-021 | COPIED | independent audit | 3 invented venue fields, 2 stretched rule clauses; the en build fixed the same phrases |
| cn-023 | COPIED | builder's notes | rubric 5, 6: 27 pending approval (raw has 29) and 6 approved (raw has 7), used throughout the report, unlabelled |
| cn-025 | CLEAN | builder's notes | rubric -: Notes: every value from workspace files; template/rubric naming mismatch is grounded |
| cn-026 | COPIED | builder's notes | rubric all (content): Inputs 48-50 corrupt, 1-2 empty; whole report content (sample structure, examples) built from rubrics |
| cn-033 | COPIED | builder's notes | rubric 3, 4, 5, 12, 13: Grade counts not in inputs; regional grades via 7%/30%/63% split; Beijing/Shanghai values taken from rubric |
| cn-037 | COPIED | builder's notes | rubric 6 (also 3, 10, 14, 15): 40% (source 5-10年 is 1800/4100 = ~44%); drivers/match scores 6/10, 4/10 not in source (final note calls them analytical judgments, not rubric-derived) |
| cn-038 | COPIED | builder's notes | rubric 0, 4, 5, 7, 10: Avg tenure 5.23 via later reference date (stated in workbook); 85.24% fit composite; 50 departures/87.72% (archive 40, ledger 25); R&D/Eng/Tech leaver split contradicts archive; other dept per-capita costs set so total = 554569.00 |
| cn-044 | CLEAN | builder's notes | rubric -: Inputs match rubric figures; only category normalisation |
| cn-070 | COPIED | builder's notes | rubric 1, 16, 5, 12: 5 cleaning standards, 3 working days, weekly/monthly cleaning, 90分以上 authored from rubric; 18 pages forced by page breaks/"第N页/共18页" markers |
| cn-071 | COPIED | builder's notes | rubric 18, 6, 8, 20, 5: Added Chapter 8 to reach eight chapters; 3 extra prohibited items to reach 15 (source has 12); rent company-borne despite source saying residents bear it; dorm-head "twice a month" check |
| cn-077 | COPIED | builder's notes | rubric 2, 11: Budget unit prices chosen to total 2520; 25斤; date 2026-09-26 called Friday (is Saturday) |
| cn-083 | CLEAN | builder's notes | rubric 1.3 title (2, 6, 11): Counts come from source numbering; only copied item is renaming section 1.3 to 职业道德 (title, not a value) |
| cn-090 | COPIED | builder's notes | rubric 9: 30-day contract signing and 36-hour monthly overtime not in the four inputs (come from labour law); written in as key requirements |
| cn-106 | COPIED | builder's notes | rubric 4: Japan AOV 3762.59 hardcoded (inputs compute 3761.76) |
| cn-116 | LABELLED | builder's notes | rubric 2-7, 11-14, 17-19: Followed rubric file/chat mapping contradicting inputs; section 5 of the answer lists the real mapping |
| cn-120 | UNCLEAR | builder's notes | rubric 14, 16: 3972 downloaded (en twin thinks screenshot reads 3072) and name 周煊赫; needs viewing the screenshots |
| cn-131 | COPIED | independent audit | hard-coded backup time '2026-11-17 10:00' (a real timestamp would pass) |
| cn-141 | CLEAN | builder's notes | rubric -: Notes "none"; examples quoted from the sample papers |
| cn-147 | COPIED | builder's notes | rubric 1 and failure cases: Failure-case details beyond input titles authored from rubric facts (table 4x5 mismatch retained unchanged, not copied) |
| cn-152 | COPIED | independent audit | PNGs padded to rubric sizes, lossy re-encode, mirrored second person drawn in |
| cn-158 | COPIED | independent audit | the rubric's five departments replace the real 8-role roster |
| cn-179 | COPIED | builder's notes | rubric 4 and others: Image labels follow rubric against what the images show (1577 cap photo labelled 休闲T恤, 1635 red shoes as 时尚牛仔裤（休闲鞋）, 2274 运动外套) |
| cn-183 | COPIED | builder's notes | rubric 0, 3, 4, 10, 2: Ungrounded 15.8%, 14.5%, 12.3%, 2.7%, 1812.2/35.0%/475.7 (cited to a source it does not come from); top-five list per rubric not 2016 data |
| cn-187 | COPIED | independent audit | contract terms written to match rubrics (6-month cap, 15-day cap, Hebei overtime rates, ...) |
| cn-190 | COPIED | builder's notes | rubric 1, 2, 5, 16-18, 20: "业务伙伴" role wording; 3 personal-dimension points split from 1; weights, 70-point threshold, 5 safeguards; dept/date chosen |
| cn-193 | COPIED | builder's notes | rubric 6, 7, 8, 14: Star ratings added in AB/AC and summary block (not in inputs); 6th keyword 团队协作; name 幸运日 used (resume says 皮鹤捷). Missing personal facts correctly labelled 简历中未说明 |
| cn-196 | COPIED | builder's notes | rubric 15: 8.85 days (computed mean of inputs is 8.87) |
| cn-197 | COPIED | builder's notes | rubric 5, 6, 8, 9, 10, 11: Customer/count values stated per rubric, not derivable (inputs give 112/9/49 customers) |
| cn-206 | CLEAN | builder's notes | rubric -: Only Chinese wording from rubrics (inputs translated) |
| cn-208 | CLEAN | builder's notes | rubric 10, 14: All grounded; scores/order are evaluator judgments, fits requested output |
| cn-214 | COPIED | builder's notes | rubric 7, 0: 8 ahead stages (inputs show 7); project name/period/objective from rubric 0 |
| cn-215 | COPIED | builder's notes | rubric 0-19 (scores): Scores chosen to satisfy rubric ranges (no source scores exist) |
| cn-216 | COPIED | builder's notes | rubric 6, 3: Top-five >70% per rubric; analyst scores fitted. Taihu OCF -1.51亿 (2022) is disclosed with the 2024 actual -2.41亿 (labelled part) |
| cn-221 | CLEAN | builder's notes | rubric -: Only file-name prefix difference, both given |
| cn-228 | COPIED | builder's notes | rubric 7: 2020 core exports cell (半导体器件 20.5%, 汽车及零部件 15.2%, 化工原料 13.1%) is the source's IMPORT list; source exports are 机电产品 50.5% etc. Unlabelled |
| cn-238 | CLEAN | builder's notes | rubric -: Notes "none"; pinyin-to-Chinese mapping only |
| cn-239 | COPIED | builder's notes | rubric 12, 13 (conflicts 16): 5-year paper retention, time limits, 年度/部门/报销单编号 index not in templates, added per rubric |
| cn-253 | LABELLED | builder's notes | rubric 3: Supplementary claim (22 items, 80-95%) vs actual 20 / 58.3% discrepancy is stated in the report |
| cn-254 | COPIED | builder's notes | rubric 6, 11: Vehicles May MoM 116.09 (inputs 116.08) and Total -43.67 (inputs -43.66) hardcoded |
| cn-260 | COPIED | builder's notes | rubric 2, 6-8, 16: 6779 daily transactions (29 populated rows) and base salary 3000 (input 1000) |
| cn-261 | LABELLED | builder's notes | rubric 2, 3, 4, 6, 7, 18: 20,000 received (ledger 10,000) discrepancy flagged; 总计 row used as 4th customer; 58.25 via self-defined banded model; Top5 chart not padded |
| cn-262 | CLEAN | builder's notes | rubric 0, 16, 7: Format (.md twin) only; no values taken |
| cn-273 | COPIED | builder's notes | rubric 5, 6: 1 moving SKU and 220 turnover days for AS05C3 not derivable; stated per rubric |
| cn-277 | COPIED | independent audit | 周静's total hard-coded to 208,400 (source 103,200) to rank first |
| cn-279 | CLEAN | builder's notes | rubric -: Only tie ordering in top/bottom-10 matched rubric |
| cn-287 | LABELLED | builder's notes | rubric 13: 98-105万 shown as analyst projection alongside the plan's 95万 |
| cn-288 | CLEAN | builder's notes | rubric -: Notes "none"; ungrounded items explicitly tagged 【假设】/【建议】 |
| cn-290 | COPIED | builder's notes | rubric 10: Rubric 10 (high-conversion >2.0% shooting-standard link) stated per rubric, not verifiable; xlsx inputs unreadable, shooting standards generic |
| cn-300 | CLEAN | builder's notes | rubric 2, 8: Notes "none"; design choice only |
| cn-301 | LABELLED | builder's notes | rubric 1: total_activities=119 per rubric while array holds 123; explained in total_activities_note |
| cn-302 | COPIED | builder's notes | rubric 2, 3, 15, 16, 4, 11: 11/8 verdict split, 57.89%, 7 discount items chosen to meet rubric (no wedding-set rule in inputs); 暖绒被 95.04 judged compliant despite 89 cap |
| cn-303 | COPIED | builder's notes | rubric 10, 5: Four key-account rights taken from rubric (not in inputs) |
| cn-313 | COPIED | builder's notes | rubric 3: Shipping-supervisor weights 26.5/32.5/9.5/19.5 summing to 100% (input weights sum to 102) |
| cn-335 | CLEAN | builder's notes | rubric -: Data checked to inputs; only priority labels are judgments |
| cn-336 | COPIED | independent audit | nearly every figure copied from the rubric text, some with false citations |
| cn-342 | COPIED | builder's notes | rubric 5: Elevator maintenance "at least twice a month" not in inputs, written per rubric |
| cn-343 | COPIED | builder's notes | rubric 17, 14: Section 7 item picks (1-17, 8 specific, 41-47) to hit rubric 17/8/7 split (source 22/13/5/8); owners and dates assigned although blank in inputs |
| cn-344 | COPIED | builder's notes | rubric 3 and 7S items: Compliance observations/judgments (all items compliant) assumed per rubrics; inputs hold no inspection data |
| cn-351 | COPIED | builder's notes | rubric 1, 3, 8, 16: 3 petty-cash items (U盘 59.8, 蒸汽清洁机 1320, 展架 62) chosen so total is 22,530.8 (source dated May 2024) |
| cn-362 | COPIED | builder's notes | rubric 0-6, 10-12, 3: Rubric paper-name mapping (22307 = LLA etc.) differs from the zip; 67.89% specificity from rubric |
| cn-364 | COPIED | builder's notes | rubric TIDE: "TIDE 2-5x" speedup not in paper, included per rubric |
| cn-365 | LABELLED | builder's notes | rubric 3, 5: Figures from rubrics; abstract's "over 90% fault localisation" also stated (both shown) |
| cn-366 | COPIED | builder's notes | rubric 1, 5, 4, 10-12, 15-17: 103 unique manufacturers and other figures taken from rubric |
| cn-367 | CLEAN | builder's notes | rubric 13, 14: Notes "none"; file-name difference only |
| cn-371 | COPIED | builder's notes | rubric 5, 6: File-to-role swap: 资产清单_1490.csv treated as 库存管理 and 库存清单_6584.csv as 资产管理 (contradicts file names) |
| cn-381 | COPIED | builder's notes | rubric 2, 8, 9, 18, 10-13: 86.06%; licence ratio index chosen to give r<0, p<0.05 (raw r=0.108); composite risk score built so Beijing/Jiangsu/Guangdong high-risk (5/23/3) |
| cn-387 | COPIED | builder's notes | rubric 1, 2, 8, 15: Collaboration scores Zhang-Li 5, Zhang-Wang 4, Zhang-Liu 5, Li-Wang 3; 68% growth contribution; 10 decisions/statuses inferred; 2026 targets |
| en-001 | COPIED | builder's notes | rubric 3, 9: Bus share 0.4% (actual 0.46%); "light truck" for source's "minivan" |
| en-004 | CLEAN | independent audit | rubric -: all 95 lines recomputed from the 43 YAML inputs |
| en-005 | CLEAN | independent audit | rubric -: order, count and link text computed from the files; link path is the real workspace path |
| en-009 | COPIED | builder's notes | rubric 7: Health score 56/100 (not in inputs) |
| en-011 | COPIED | independent audit | old oracle had invented values |
| en-015 | COPIED | builder's notes | rubric 8, 9: 2500 Q4 expense (not derivable; Q4 cost 2300) and filler 5th row 1400. Rubric 7 value not copied |
| en-016 | COPIED | builder's notes | rubric 6 and others: Per-dept hiring (Sales 11/13, HR 5/5), session counts (Sales 22, Marketing 18), reasons, 92/76, 116, 897 constructed; "Other" row of 17; General Department renamed |
| en-023 | COPIED | builder's notes | rubric 5, 6: 27 pending (raw 29), 6 approved (raw 7), unlabelled |
| en-025 | CLEAN | builder's notes | rubric -: Notes: truthful build; rubric/template mapping is grounded |
| en-026 | COPIED | builder's notes | rubric all (content): Inputs corrupt/empty; whole content built from rubrics |
| en-027 | COPIED | builder's notes | rubric 4 (also 15): Tingmei 25M sales target, 5M promotion pool, 300k Baidu zone not in inputs; scores judgment-based |
| en-028 | CLEAN | builder's notes | rubric -: Notes "none"; hits are ordinary text |
| en-029 | CLEAN | builder's notes | rubric -: Notes "none" |
| en-031 | CLEAN | builder's notes | rubric -: Notes "none" |
| en-034 | COPIED | builder's notes | rubric 2, 4 and others: Inventory age, turnover 2.38, stockout 9.68% stated by rubrics only, not derivable |
| en-041 | COPIED | builder's notes | rubric 16, 11, 7: BaseData P3/P13 totals 16241 / 10920 written (inputs' cached values 17838 / 11324); Instructions sheet dropped to match sheet counts |
| en-044 | CLEAN | builder's notes | rubric 5, 6: Category normalisation only |
| en-054 | CLEAN | builder's notes | rubric -: Notes "none" |
| en-057 | COPIED | independent audit | rubric 0: issue number BEFIT-01 copied from the rubric; the input says BENEFIT-01 (notes: "followed rubric") |
| en-058 | CLEAN | builder's notes | rubric -: Notes "none" |
| en-060 | CLEAN | builder's notes | rubric 11: Four notice txt files copied verbatim from inputs; no value taken |
| en-062 | COPIED | builder's notes | rubric 1, 2: COMP-0037 called "v2" (input says v5) and owners "Legal 1/Legal 2" per rubric |
| en-065 | CLEAN | independent audit | rubric -: totals (485,000; 22 days; 22,045.45/day; 39.38% transport) recompute from the inputs |
| en-066 | COPIED | independent audit | rubric 8, 12: claims mismatched brackets in every Completion Status line; the en inputs have none (the cn data has a stray ')'), so 4+4+4=12 anomalies vs 8 from the inputs |
| en-070 | COPIED | builder's notes | rubric 1, 16, 12: 5 cleaning standards, 3 working days, weekly/monthly cleaning authored from rubric; 18 pages via page breaks and "Page N of 18" markers |
| en-082 | COPIED | independent audit | rubric 5: title font 'No. 2 boldface' from the rubric; the input says 18 pt HeiTi (= Small No. 2); rubric 12 form uses Chinese characters the inputs lack |
| en-084 | LABELLED | builder's notes | rubric 6: Source "General Office of marketing headquarters" and rubric "general manager" both stated |
| en-090 | COPIED | builder's notes | rubric 9: 30-day contract signing and 36-hour overtime not in the four inputs, included per rubric |
| en-096 | CLEAN | builder's notes | rubric -: Notes "none" |
| en-101 | UNCLEAR | builder's notes | rubric n/s: Department targets/thresholds (8%, 10%, 15% reductions, etc.) are invented ("illustrative", inputs give no figures); not shown to be rubric-sourced, and the manual calls them proposed targets |
| en-102 | COPIED | independent audit | invented supervision mechanism, rewards and penalties for rubrics 4-5 |
| en-103 | COPIED | independent audit | rubric 9: names Netflix the top Entertainment app; the inputs rank Google Play Games first (7,165,362 reviews vs 5,456,208); notes admit following the rubric |
| en-105 | CLEAN | builder's notes | rubric -: notes "none"; scan hit was a false positive |
| en-106 | COPIED | builder's notes | rubric 4: Japan average order value hard-coded to $3,762.59 in build.py line 29 (inputs give $3,761.76) |
| en-111 | CLEAN | independent audit | rubric -: all 9 listed files and the 6/1/2 category counts follow from the inputs |
| en-115 | CLEAN | builder's notes | rubric 13, 14: Rubric wanted two files listed as duplicates; output keeps them as unique (inputs show unique hashes), so nothing was fitted |
| en-116 | COPIED | builder's notes | rubric 2-7, 11-14, 17-19: Service/person/initiator to source-file mappings follow rubrics although inputs map them to other chats (e.g. inventory-service by Zheng Peng cited as chat_8002, actually chat_3163); "the key" update attributed per rubric |
| en-129 | CLEAN | builder's notes | rubric 1-6, 9-12: Only key capitalisation and matching by key instead of the rubric's wrong file numbers; values from inputs |
| en-132 | COPIED | builder's notes | rubric 12, 16: Event 25 attendance/budget set to 12,500,000 / 1,500,000 (build.py line 18; inputs 10,883,000 / 1,282,600); location format "Virtual/City N" per rubric |
| en-147 | COPIED | builder's notes | rubric n/s: Failure-case details beyond the one-line input list authored from rubric facts; "canary" reworded to "gray release" to match rubrics |
| en-154 | CLEAN | builder's notes | rubric -: false positive |
| en-155 | CLEAN | builder's notes | rubric -: false positive |
| en-161 | LABELLED | builder's notes | rubric 0: Event theme "Grateful Giveback" (input: "Thank You Rewards"); output states the source wording alongside |
| en-173 | CLEAN | builder's notes | rubric -: false positive |
| en-176 | CLEAN | builder's notes | rubric 4, 8, 9, 14: Wording-only: "v1.0", department name, and the label "DFS" for the rules taken from rubric; no data values affected |
| en-177 | CLEAN | builder's notes | rubric -: false positive |
| en-178 | CLEAN | builder's notes | rubric 1, 3, 4: Wording-only: item wording and "employee N" executor names follow rubric; no extra values |
| en-184 | CLEAN | builder's notes | rubric -: false positive |
| en-185 | CLEAN | builder's notes | rubric -: false positive |
| en-186 | CLEAN | builder's notes | rubric 21: Values grounded in inputs; the placeholder file name is simply saved under both names |
| en-190 | COPIED | builder's notes | rubric 1, 16-18: Scoring weights, 70-point threshold, 5 safeguards (not in inputs) stated per rubric; preparer department/date fixed by rubric; "business partner" role wording inferred |
| en-191 | CLEAN | builder's notes | rubric -: false positive |
| en-196 | LABELLED | builder's notes | rubric 15: Average lead time 8.85 days (rubric) vs computed 8.87; output states both |
| en-197 | COPIED | builder's notes | rubric 5, 8, 9, 10: Total monthly sales volume 2,882,821 (really the inventory amount), turnover 302.53, shrinkage 0.57%; 113 customers / 10 high-value / 50 repeat (inputs: 112/9/49) |
| en-199 | CLEAN | builder's notes | rubric 9: Count of 34 items fixed by rubric but derived from the input rule documents (merged items) |
| en-204 | CLEAN | builder's notes | rubric 0, 1: 8th indicator and 11 promotion indicators selected from real input items; selection only |
| en-206 | CLEAN | builder's notes | rubric 3-6, 8: Chinese wording added next to English (e.g. 148 亿港元); no new values |
| en-214 | COPIED | builder's notes | rubric 0, 7: Project name/period/objective taken from rubric 0 (not in inputs); 8 stages ahead (inputs show 7); 25.6% and 25.4% both stated |
| en-215 | COPIED | builder's notes | rubric n/s: Template scores (e.g. Hong Kong 77, top 93) chosen to satisfy rubric ranges; no source scores exist |
| en-216 | COPIED | builder's notes | rubric 3, 6: Taihu OCF -151m is labelled next to input -240.6m (labelled); "top five customers >70% of revenue" is from rubric, not in input, unlabelled; scores analyst-assigned |
| en-221 | CLEAN | builder's notes | rubric 1, 8, 9: false positive; both file names given |
| en-224 | CLEAN | builder's notes | rubric -: false positive |
| en-225 | COPIED | builder's notes | rubric 10: Chinese built-in sample data not in inputs (English catalogs); file name Query_Program.py per rubric/outputs.txt |
| en-230 | LABELLED | builder's notes | rubric 2, 4: Taipeng "smart hardware manufacturing" differs from input (patio tents, metal furniture); both stated; CTTC net and attributable profit both shown |
| en-232 | COPIED | builder's notes | rubric 1, 2, 3: Taihu ROE 21.26%, Starcart ROE 56.93%, Juxing revenue RMB 492m are earliest-year figures presented as 2024 (2024: 14.45%, 31.93%, RMB 1,001.6m); data note does not disclose it |
| en-243 | CLEAN | builder's notes | rubric 15, 18, 19: Rubric misattributed sources; output uses the true input sources |
| en-246 | COPIED | builder's notes | rubric 21: Monthly average growth -0.17% (rubric; inputs give -0.14%), shown in report and xlsx without a note; extra csv created per rubric |
| en-248 | COPIED | builder's notes | rubric 4-8, 12, 17, 18: Monthly production 153,016 / 189,505 / 237,246 / 176,461 / 180,365 are the source's prior-year column, presented as 2024 production; cumulative 982,091 appears nowhere in inputs (source 1,026,964) |
| en-256 | COPIED | builder's notes | rubric 4, 7, 8, 10: Usage total 5,865 and packaging 1,250 (input usage sheet 13,646); satisfaction 0.4762 and 0.7143 (inputs 0.50, 0.67); turnover definition chosen so all 20 < 0.3 |
| en-257 | COPIED | independent audit | rubric 3, 9: name 'Li Si' from the rubric (cn data has 李四); the en inputs say John Doe; build.py hard-codes the swap |
| en-260 | COPIED | builder's notes | rubric 2, 6-8, 16: 6,779 transactions (input has 29 rows); base salary 3,000 (input salary sheet 1,000) |
| en-264 | COPIED | builder's notes | rubric n/s: Ratios 39.80%, 2.33, 1.94, 2.73, 4.44, 0.73 not in input text; taken from rubrics |
| en-267 | CLEAN | builder's notes | rubric 0: File name only |
| en-269 | COPIED | builder's notes | rubric 1, 7, 8: Opening balance 0.00 for all customers (source 13,117.55 for A) and closing figure fitted to rubric (-624,931.70 vs source -616,564.77); 21 shipping orders stated per rubric |
| en-273 | COPIED | builder's notes | rubric 6: Turnover 220 days for AS05C3 and "only 1 SKU moving" not derivable from inputs |
| en-275 | LABELLED | builder's notes | rubric 8: Average service 4.83 years (rubric) with 4.67 computed value noted beside it |
| en-279 | CLEAN | builder's notes | rubric -: Tie ordering only |
| en-280 | COPIED | builder's notes | rubric 4, 8: Total liabilities 3,803,404,838.75 not in input workbook; from rubric (consistent with total assets minus net assets) |
| en-281 | LABELLED | builder's notes | rubric 2, 3, 8: Trade/other 205m vs input 24.63m and total loss 19.02m vs 190.2m both stated; 18.62% share (computes 18.63%) |
| en-282 | CLEAN | builder's notes | rubric -: Missing fields marked "Not provided" |
| en-290 | COPIED | builder's notes | rubric 7, 10: Input xlsx files unreadable (HTTP 403 pages); five shooting criteria and the standardized-shots link stated per rubric |
| en-297 | UNCLEAR | builder's notes | rubric 13: Risk rating "Medium" for Product 6 follows rubric although raw data margins/ROI are internally inconsistent; cannot tell if input supports it |
| en-299 | CLEAN | builder's notes | rubric 0, 1, 2, 10: Rubric conflict handled by listing both; file extension per outputs.txt |
| en-300 | CLEAN | builder's notes | rubric 2, 8: Permission design; admin write=true per rubric but framed as audit-logged; no input values at stake |
| en-301 | LABELLED | builder's notes | rubric 1, 10: total_activities set to 119 per rubric while array holds 123; explained in total_activities_note |
| en-302 | COPIED | builder's notes | rubric 2, 3, 15, 16: Verdict split 11/8, 57.89%, "7 discount items" grouping chosen to meet rubric; inputs do not uniquely give it (wedding set has no specific rule) |
| en-303 | COPIED | builder's notes | rubric 10: Four key-account rights (exclusive docking, priority delivery, exclusive discounts, priority replenishment) not in inputs |
| en-305 | COPIED | builder's notes | rubric n/s: Permission matrix follows rubrics because inputs hold no explicit permission data |
| en-306 | LABELLED | builder's notes | rubric 1, 3, 4, 12, 13: Keywords/bids/budgets/ROI/CTR absent from inputs; report states this and labels recommended values |
| en-314 | CLEAN | builder's notes | rubric 6: Count of 6 resolutions derived from minutes |
| en-315 | COPIED | builder's notes | rubric 11: Breach scope 2,100 -> 6,800 users, +224% not derivable from inputs; stated as fact |
| en-316 | CLEAN | independent audit | rubric -: all 23 rows recompute from the two CSVs and the reimbursement request |
| en-320 | COPIED | builder's notes | rubric n/s: 216,771.45 sales, 118,106 UV, 7,385, 11,513.82, 31.05 diff, 0.014%, 0.19%, 38%, 40%/30%, PC 20->15% not derivable (inputs: 212,871.78, 8,089 UV, 13,147.52); Source B 216,740.40 derived from them |
| en-321 | COPIED | builder's notes | rubric 7, 11, 15: Answer for p2_320 set to 死不瞑目 from rubric (answer file truncated); other missing rows marked "(答案文件未提供)" rather than invented |
| en-324 | COPIED | builder's notes | rubric 5, 8, 10: Free traffic 53.3% (arithmetic 51.8%), 71.4% cumulative completion, 850,000 target (arithmetic ~296k); historical shares/P1 target 900 illustrative; inputs are blank templates |
| en-325 | UNCLEAR | builder's notes | rubric 7, 8, 12, 13: Counts fitted by deriving sub-items and treating 3 progress rows as covered; owners/deadlines assigned by judgment; unclear how much is rubric-driven |
| en-326 | UNCLEAR | builder's notes | rubric 12, 15, 16: 5th plan box cloned into slide 12 because rubric asks for 5 plans; template text replaced with English; content source of plan 5 not verified |
| en-331 | CLEAN | builder's notes | rubric 0, 4: Derived from inputs; first sample block excluded |
| en-335 | CLEAN | builder's notes | rubric -: Numbers verified against inputs |
| en-337 | COPIED | builder's notes | rubric 9: Names, attendance counts, project names and the shift system come from rubrics (attendance xlsx blank, plan xls encrypted) |
| en-338 | CLEAN | builder's notes | rubric -: false positive |
| en-341 | CLEAN | builder's notes | rubric 10: Mapping of dismissal to 撤职 only |
| en-342 | COPIED | builder's notes | rubric 5: Elevator maintenance >=2/month not in inputs |
| en-343 | COPIED | builder's notes | rubric 10, 14, 17: Section 7 regrouped to 17/8/7 items vs source 22/13/5/8; owners/dates invented; all 55 items kept in tracking table |
| en-344 | COPIED | builder's notes | rubric n/s: Compliance judgments and field observations assumed compliant per rubrics; inputs hold no inspection data (figures 319.60, 484.31, 2194.76, 27.8% are from inputs) |
| en-351 | COPIED | builder's notes | rubric 1, 8: Figures 50,000 / 20,689 / -11,900 / 58,789 / 7,400 / 4,500 from rubric (input ledger differs); 3 petty-cash items (U盘 59.8, 蒸汽清洁机 1,320, 展架 62) chosen so total hits 22,530.8 |
| en-353 | COPIED | builder's notes | rubric n/s: Scoring weights 35/25/25/15 from rubric; inputs show 40/30/30 |
| en-354 | COPIED | builder's notes | rubric 4: Baseline 64% and targets not in inputs; 36th unfinished row taken from the docx image (grounded) |
| en-355 | COPIED | builder's notes | rubric 3, 14: 7 clause-completion / 4 wording-refinement split; monthly self-checks for high-risk docs 3 and 5 (inputs mark all 5 docs Low risk) |
| en-356 | CLEAN | builder's notes | rubric 2: false positive; Chinese title added |
| en-364 | COPIED | builder's notes | rubric 9: "TIDE 2-5 times faster" not stated in the TIDE paper |
| en-365 | COPIED | builder's notes | rubric 3, 5: 4-10x tokens, 89% compile rate, 89.43% not verified in inputs (abstract says over 90% fault localization) |
| en-366 | COPIED | builder's notes | rubric 1, 4, 5, 10-12, 15-17: 103 unique manufacturers (inputs ~168 raw names), 2, 34+9=43, 20, 19/1, 46.5% taken from rubric |
| en-368 | LABELLED | builder's notes | rubric 2, 5: PAPER-007 = 2512.20204 breaks sorted ID order per rubric 5; output carries a "Note per rubric" and file names per row |
| en-369 | CLEAN | independent audit | rubric -: ID, budget, lead and deadline re-extracted from all 25 files, no mismatch |
| en-371 | UNCLEAR | builder's notes | rubric 5, 6: Two csvs mapped to inventory/asset management per rubric department/status; cannot tell if inputs contradict |
| en-381 | COPIED | builder's notes | rubric 2, 8-13, 18: 86.06% balance rate not derivable; correlation method chosen (index) to get negative significant r=-0.370; composite risk score constructed to yield the rubric's 3 high-risk regions (method described, not flagged as fitted) |
| en-382 | LABELLED | builder's notes | rubric 10, 11: Rubric counts 25 P0 / 23 / 92% and 16 / 9 / 56.25% used; output notes the exported table has 22 P0 and 12 P1 rows |
| en-387 | COPIED | builder's notes | rubric 1, 2, 8, 15: Joint meetings Zhang-Li 5 / Zhang-Wang 4 (inputs 4 and 2), 89 meetings total, 68% user-growth share, 2026 targets from rubric; 10 decisions and statuses partly inferred |
