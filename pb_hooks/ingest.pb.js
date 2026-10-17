/// <reference path="../pb_data/types.d.ts" />
// Write rules, ownership, ingestion runs and history. The logic is in ingest.js.
const WRITABLE = ['organizations', 'profiles', 'positions', 'education', 'posts', 'comments', 'reactions', 'feed_items',
  'notifications', 'connections', 'invitations', 'follows', 'conversations', 'conversation_participants', 'messages',
  'aliases', 'accounts', 'crm_links', 'ingestion_runs'];
onRecordValidate((e) => { e.next(); require(`${__hooks}/ingest.js`).validate(e.app, e.record); }, ...WRITABLE);
onRecordCreateRequest((e) => require(`${__hooks}/ingest.js`).write(e), ...WRITABLE);
onRecordUpdateRequest((e) => require(`${__hooks}/ingest.js`).write(e), ...WRITABLE);
onRecordDeleteRequest(() => { throw new ForbiddenError('Records are retained; ingestion never deletes. Ask the operator for erasure.'); }, ...WRITABLE);
onRecordCreateExecute((e) => require(`${__hooks}/ingest.js`).audit(e, 'create'), ...WRITABLE);
onRecordUpdateExecute((e) => require(`${__hooks}/ingest.js`).audit(e, 'update'), ...WRITABLE);
