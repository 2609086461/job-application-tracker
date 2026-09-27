# Review Library Configuration

Each user must use their own Feishu folder and review-index table. Do not copy another user's tokens.

Recommended private environment variables:

```text
FEISHU_REVIEW_ROOT_FOLDER_TOKEN=
FEISHU_REVIEW_INDEX_APP_TOKEN=
FEISHU_REVIEW_INDEX_TABLE_ID=
```

Recommended folder layout:

```text
秋招复盘库/
  公司名称/
    求职资料总览
    01 面试准备/
    02 面试复盘/
```

The index should at minimum contain record name, type, company, position, round, date, status, preparation-document link, review-document link and next action. The exact field names may be adapted once and then documented in the user's private configuration.
