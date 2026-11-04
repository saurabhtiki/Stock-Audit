# **Stock Audit Web Application: Requirements Document** 

## **1. Purpose and scope** 

The app manages the group's stock audits across multiple companies and multiple Branches. It records ERP stock, the auditor's physical count, and the variances between them, then tracks queries, replies, and actions. 

- **Audit:** one audit is identified by Branch + Title (for example, "Branch A – Aug26", dated 30 Aug), labelled Scheduled or Surprise. The title is unique within a Branch; a Branch can have several audits in a month, even on the same date. 

- **Notifications:** in-app only. No email or SMS in this version. 

- **Offline:** auditors can count without internet using an Excel count sheet (section 4). 

- **Start:** the app starts fresh. No historical data is imported. 

## **2. Users, roles and rights** 

Each user has one username(combine unique with company) and password login with email. Every user is created by Admin, who also resets passwords. Admin sets rights per user and per Branch, so one user can have different rights in different Branches. Users are deactivated, never deleted. 

|**Role**|**Default description**|
|---|---|
|Admin|Manages companies, Branches, users, and rights. Decides who can reply to queries<br>and handle actions in each Branch. Reopens locked audits. Downloads backups.|
|Manager|Manages assigned Branches. Sets tolerance and freezes ERP stock, if granted. Adds<br>actions to queries. Locks audits, if granted.|
|User|Uploads ERP stock, replies to queries, and marks actions done, if granted.|
|Auditor|Enters physical counts, remarks, and media. Marks Checking Done. Does not get ERP<br>upload or tolerance rights by default.|
|Viewer|Read-only access to assigned Branches.|



The default role sets starting rights. Admin can change them for any user and Branch. 

#### **Rights (set per user and Branch):** 

- upload ERP stock 

- set tolerance and freeze ERP stock 

- enter count and remarks; mark Checking Done (Auditor) 

- upload photos, videos and files (Auditor) 

- reply to queries 

- create and assign actions; mark actions done 

- verify actions and close queries 

- lock audit 

- view reports and dashboards 

Users see only the Branches they have rights to. To save effort, Admin can apply a user's rights to all Branches of a company in one step, or copy rights from another user. 

## **3. Master data and ERP upload** 

### **Master data** 

- **Companies:** name and status (active or inactive),logo,address. Admin adds, edits, and deactivates. 

- **Branches:** company, name, code, and status. Admin adds, edits, and deactivates. 

- **Items:** item code (unique within a company), description, one unit of measure, and status. 

- **New items:** codes in an ERP upload that are not in the master list are added automatically, marked "New", and used immediately. Manager reviews them later. 

### **Creating an audit and uploading ERP stock** 

1. A user with upload right creates the audit: Branch, title, audit date, type (Scheduled or Surprise), and "ERP stock as on" date (mandatory). 

2. The user downloads the generic Excel/CSV template and copies from the ERP report into: Item No, Description, UoM, ERP Qty, Rate p.u. 

3. The user uploads the file. The app shows a preview. Errors block saving: non-numeric quantity or rate, duplicate item codes. Warnings need a tick to continue: negative ERP quantities. Nothing is saved until the user confirms. 

4. On confirmation, lines are saved as the **ERP Draft** . Total value = ERP Qty × Rate. 

While the audit is in ERP Draft, the file can be replaced by uploading again. Once ERP stock is frozen (section 4), it can no longer be changed. 

## **4. Audit stages, count, remarks, queries and actions** 

### **Audit stages** 

|**Stage**|**What happens**|**Who**|**After this stage**|
|---|---|---|---|
|1. ERP Draft|ERP file uploaded; can be replaced.|User with upload<br>right|Free to change.|
|2. ERP<br>Frozen|Tolerance set, ERP stock frozen<br>after a confirmation screen. Counting<br>starts.|Manager with right|ERP lines and tolerance<br>cannot be changed.|
|3. Checking<br>Done|Auditor confirms the count is<br>complete. Queries are generated.|Auditor|Counts cannot be edited.<br>No one, including Admin,<br>can roll this back.|
|4. Locked|Audit is closed.|User with lock<br>right|Frozen. Only Admin can<br>reopen, with a required<br>reason that is logged.<br>Reopening returns the audit<br>to Checking Done.|



Every freeze step shows a confirmation screen with a summary first (for example, number of lines counted and not counted, and number of excess and shortage lines). If a wrong ERP file is frozen before any count is entered, Admin can cancel that audit with a logged reason and a new audit can be created. 

### **Tolerance** 

- Set per audit by a Manager with the right, before ERP stock is frozen. Admin grants this right. 

- Two optional settings: **quantity tolerance** and **percentage tolerance** (variance as % of ERP Qty). Default is 0 for quantity and not set for percentage. 

- A line is flagged if its variance exceeds **either** tolerance that is set. If ERP Qty is 0 and the count is not, the percentage test counts as exceeded. 

### **Physical count (auditor)** 

- The auditor opens the frozen lines of the audit and searches by item code or description. Filters show Not counted, Excess, and Shortage lines. Each line saves as it is entered. 

- The auditor enters Physical Qty. A blank means "not counted yet"; zero is a valid count. 

- The app calculates Variance Qty = Physical − ERP, Variance Value = Variance Qty × Rate, and Status: **Excess** , **Shortage** , **OK** , or **Not counted** . 

- Lines still blank at Checking Done stay as "Not counted" and are listed in reports. They do not raise queries. 

### **Offline count sheet (Excel)** 

- Once ERP stock is frozen, the auditor downloads a count sheet for the audit with Item No, Description, UoM, Physical Qty, and Remark. ERP quantity is not shown, so the count is blind. 

- After counting manually, the auditor uploads the sheet. A preview shows errors (unknown item codes, non-numeric quantities). Nothing is saved until the auditor confirms. 

- Blank cells in the sheet never overwrite counts already entered in the app. Upload is allowed only until Checking Done. 

- Photos, videos, and files are added later by the auditor. 

### **Remarks** 

Remarks are informational only and never raise a query. 

- **Line remarks** on any count line, such as "bin mismatch". 

- **General remarks** on the whole audit, such as "no security at the gate". 

- Time-stamped and attributed. Editable or deletable until the audit is locked. 

### **Queries** 

- Generated automatically at Checking Done, one for each Excess or Shortage line. 

- One reply per query, with optional updates later. Admin decides, per Branch, who can reply. 

|**Status**|**Meaning**|
|---|---|
|Open|Query raised, no reply yet. Also the status of a reopened query.|
|Reply Submitted|A reply has been posted and is waiting for review.|
|Action Pending|At least one action is added and not yet verified.|
|Closed|All actions are verified (or none were needed) and a user with close right has<br>confirmed. Reopening sets the status back to Open, and the log records it.|



- **Carry forward:** a query that is not Closed stays live and appears in the Branch's next audit. Replies, actions, and closure continue there. 

- **Item history table:** each query page shows one row per audit for that item (audit date, ERP Qty, Physical Qty, Variance, and query status). A new query raised in a later audit for the same item appears in this table, so a repeating problem over five or six audits can be seen in one place. 

### **Actions** 

- Manager adds one or more actions to a query: description, owner (any user), optional due date, and status Open, Done, or Verified (default is Open). 

- Admin assigns, per Branch, who creates actions, who marks them done, and who verifies them. 

- A query closes when all its actions are verified and a user with close right confirms it. 

## **5. Media, files, locking, surprise checks and dashboards** 

Media and files attach to the audit, a count line, a query, or an action. They are kept on server disk with no time limit; the database stores only the path. Only the allowed file types below are accepted. 

|**Type**|**Limit**|**Notes**|
|---|---|---|
|Photos|10 MB each|Compressed on upload. A tick "include in report" chooses<br>which photos go into the PDF.|
|Videos|100 MB each|Common video formats only.|
|Other files|5 MB each|PDF, DOCX, XLSX only.|



**Locking:** a user with lock right locks an audit once it is at Checking Done. Locking freezes counts, remarks, and media, and closed queries with their actions. Queries that are not Closed at that moment stay live and carry forward (section 4). Only Admin can reopen a locked audit, with a required reason that is logged. 

**Surprise checks:** a surprise check is an audit labelled Surprise, with its own date. It follows the same stages as a scheduled audit and is reported separately. 

**Dashboards and alerts:** in-app only. Interactive, filterable 

- **My pending items:** each user's home page lists the replies, actions, and verifications waiting for that user. 

- **Audit status grid:** every audit with Branch, date, type, stage, percentage counted, and open queries, so Admin can see at a glance what is pending across all Branches. 

- Open queries by Branch, overdue actions, and ageing of open queries. 

- Excess value, shortage value, and net variance (excess minus shortage) by Branch and month. 

- Repeat items: the same item with a variance in consecutive audits. 

- History/Trend: past queries/variance trend by item/branch/ 

## **6. Reports, audit trail, platform, technology and acceptance** 

**Reports:** all data exportable to Excel (lines, variances, remarks, queries, actions, activity log). One PDF per audit, for printing, with count summary (including Not counted lines), variances, line and general remarks, query and action status, selected photos, and a signature block for the Auditor, Stockkeeper, and Manager. 

**Audit trail:** every change is logged with user, date and time, record, old value, and new value. No one, including Admin, can edit or delete the log. 

**Platform:** responsive web app for phones and computers; username and password login; HTTPS over office network and internet. 

**Technology:** Django with HTMX and DaisyUI. SQliteDB for development, testing, and production. Media on server disk. 

**Backup:** Admin has a "Download backup" button that produces a zip, either database only or database plus media. The Admin screen shows when the last backup was downloaded and a reminder banner appears after 7 days. 

### **Acceptance criteria** 

Version 1 is accepted after a pilot with one company and two Branches, where all of the following work: 

1. Admin creates users and sets different rights for the same user in two Branches. 

2. A user creates an audit, uploads an ERP file with errors, corrects it, replaces the draft, and confirms. 

3. A Manager sets tolerance (quantity and percentage) and freezes ERP stock; the ERP lines can no longer be changed. 

4. The auditor counts in the app and through the offline Excel count sheet, and some lines are left Not counted. 

5. At Checking Done, queries are created only for Excess and Shortage lines, and counts can no longer be edited or rolled back. 

6. A query goes through Open, Reply Submitted, Action Pending, and Closed, with an action owner and verifier. 

7. An open query carries into the next audit and shows the item history table. 

8. The audit is locked; Admin reopens it with a reason; every step appears in the activity log. 

9. The PDF and Excel exports match the data on screen. 

10. Admin downloads a backup zip and it restores correctly on a test server. 

### **Out of scope for version 1** 

- Email, SMS, or push notifications. 

- A true offline app that syncs automatically (the Excel count sheet is used instead). 

- Import of historical audit data. 

- Checks for rate changes, unit changes, or missing items in the ERP upload. 

