// Write rules for NetworkContext. Every hook handler runs in its own runtime, so handlers load this with require().
const ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';
// Collections whose records are keyed by a LinkedIn natural key and written by ingestion.
const KEYED = ['organizations', 'profiles', 'positions', 'education', 'posts', 'comments', 'reactions', 'feed_items',
  'notifications', 'connections', 'invitations', 'follows', 'conversations', 'conversation_participants', 'messages'];
const OWNED = ['feed_items', 'notifications', 'connections', 'invitations', 'follows', 'conversations', 'ingestion_runs'];
const AUDITED = KEYED.concat(['aliases', 'accounts', 'crm_links', 'ingestion_runs']);
const MANAGED = ['created_by', 'updated_by', 'created', 'updated'];
// Observation bookkeeping and engagement counters change on every capture; they are not history.
const UNAUDITED = ['updated', 'updated_by', 'first_seen_at', 'last_seen_at', 'last_run',
  'reaction_count', 'comment_count', 'repost_count', 'counts'];
const RUN_FIELDS = ['status', 'counts', 'reached_cutoff', 'note'];

function invalid(message) { throw new BadRequestError(message); }

// The same derivation is implemented by the client (networkcontext_client.keys.record_id).
function recordId(collection, naturalKey) {
  const hex = $security.sha256(collection + ':' + naturalKey);
  let id = '';
  for (let i = 0; i < 15; i++) id += ALPHABET[parseInt(hex.substr(i * 2, 2), 16) % 36];
  return id;
}

function now() { return new Date().toISOString().replace('T', ' '); }

function actor(e) {
  return e.auth && e.auth.collection().name === 'users' ? e.auth.id : '';
}

function has(body, field) { return Object.prototype.hasOwnProperty.call(body, field); }

function requireOwnAccount(app, e, accountId) {
  const user = actor(e);
  if (!user) return; // Superusers provision and maintain.
  if (!accountId) invalid('account is required');
  const account = app.findRecordById('accounts', accountId);
  if (account.getString('user') !== user) throw new ForbiddenError('This LinkedIn account belongs to another user.');
}

function validate(app, r) {
  const name = r.collection().name;
  const exactlyOne = (a, b, label) => {
    if (!!r.getString(a) === !!r.getString(b)) invalid(label + ' requires exactly one of ' + a + ' and ' + b);
  };
  if (name === 'reactions') exactlyOne('post', 'comment', 'A reaction');
  if (name === 'follows') exactlyOne('profile', 'organization', 'A follow');
  if (name === 'aliases') exactlyOne('profile', 'organization', 'An alias');
  if (name === 'posts' && r.getString('author_profile') && r.getString('author_organization')) invalid('A post has one author');
  if (name === 'posts' && r.getString('reshare_of') === r.id) invalid('A post cannot reshare itself');
  if (name === 'comments' && r.getString('parent')) {
    const parent = app.findRecordById('comments', r.getString('parent'));
    if (parent.getString('post') !== r.getString('post')) invalid('A reply must belong to the same post');
  }
  if (name === 'profiles' && r.getString('merged_into') === r.id) invalid('A profile cannot merge into itself');
}

// Create and update requests for keyed, owned and manual collections.
function write(e) {
  const r = e.record, name = r.collection().name, fresh = r.isNew(), body = e.requestInfo().body, user = actor(e);
  for (const field of MANAGED) if (has(body, field)) invalid(field + ' is server managed');
  if (KEYED.includes(name)) {
    const key = r.getString('natural_key');
    if (!key) invalid('natural_key is required');
    const expected = recordId(name, key);
    if (fresh) {
      if (has(body, 'id') && body.id !== expected) invalid('id must be derived from natural_key');
      r.id = expected;
    } else {
      if (key !== r.original().getString('natural_key')) invalid('natural_key is immutable');
      // Keep the earliest and latest observation whatever order captures arrive in.
      const old = r.original();
      const first = old.getString('first_seen_at'), last = old.getString('last_seen_at');
      if (first && (!r.getString('first_seen_at') || r.getString('first_seen_at') > first)) r.set('first_seen_at', first);
      if (last && r.getString('last_seen_at') < last) r.set('last_seen_at', last);
    }
    const run = r.getString('last_run');
    if (run && (fresh || run !== r.original().getString('last_run'))) {
      const record = e.app.findRecordById('ingestion_runs', run);
      if (record.getString('status') !== 'running') invalid('last_run must be a running ingestion run');
      if (user && e.app.findRecordById('accounts', record.getString('account')).getString('user') !== user)
        throw new ForbiddenError('last_run belongs to another user.');
    }
  }
  if (OWNED.includes(name)) {
    if (!fresh && r.getString('account') !== r.original().getString('account')) invalid('account is immutable');
    requireOwnAccount(e.app, e, r.getString('account'));
  }
  if (name === 'messages' || name === 'conversation_participants') {
    const conversation = e.app.findRecordById('conversations', r.getString('conversation'));
    requireOwnAccount(e.app, e, conversation.getString('account'));
  }
  if (name === 'accounts') {
    if (user) r.set('user', user);
    if (!fresh && r.getString('profile') !== r.original().getString('profile')) invalid('profile is immutable');
  }
  if (name === 'ingestion_runs') run(e, fresh, body);
  r.set('created_by', fresh ? user : r.original().getString('created_by'));
  r.set('updated_by', user);
  r.set('_audit_actor', user ? 'user:' + user : 'superuser:' + (e.auth ? e.auth.id : ''));
  return e.next();
}

function run(e, fresh, body) {
  const r = e.record;
  if (fresh) {
    for (const field of ['status', 'started_at', 'completed_at', 'reached_cutoff', 'counts'])
      if (has(body, field)) invalid(field + ' is server managed on start');
    r.set('status', 'running');
    r.set('started_at', now());
    const running = e.app.findRecordsByFilter('ingestion_runs', "account = {:account} && surface = {:surface} && status = 'running'", '', 1, 0,
      {account: r.getString('account'), surface: r.getString('surface')});
    if (running.length) throw new ApiError(409, 'Another run for this account and surface is still running (' + running[0].id + '); finish or fail it first.', {});
    return;
  }
  const old = r.original();
  if (old.getString('status') !== 'running') invalid('Finished runs are immutable');
  for (const field in body) if (field !== 'id' && !RUN_FIELDS.includes(field)) invalid(field + ' cannot change after a run starts');
  const status = r.getString('status');
  if (status === 'running') return;
  r.set('completed_at', now());
  if (status !== 'complete' || !r.getBool('reached_cutoff')) return;
  // A complete run that reached its cutoff proves coverage up to its start.
  const found = e.app.findRecordsByFilter('sync_state', 'account = {:account} && surface = {:surface}', '', 1, 0,
    {account: r.getString('account'), surface: r.getString('surface')});
  const state = found.length ? found[0] : new Record(e.app.findCollectionByNameOrId('sync_state'));
  if (found.length && state.getString('high_water_at') >= r.getString('started_at')) return;
  state.set('account', r.getString('account'));
  state.set('surface', r.getString('surface'));
  state.set('high_water_at', r.getString('started_at'));
  state.set('last_complete_run', r.id);
  e.app.save(state);
}

// Run the request hook inside one transaction, so the run completion and its sync state commit together.
function transactional(e) {
  const original = e.app;
  original.runInTransaction((app) => {
    e.app = app;
    try { write(e); } finally { e.app = original; }
  });
}

function snapshot(r) {
  const all = JSON.parse(JSON.stringify(r)), out = {};
  for (const f of r.collection().fields.fieldNames()) out[f] = all[f];
  return out;
}

function audit(e, action) {
  const who = e.record.getString('_audit_actor');
  if (!who) return e.next();
  e.record.set('_audit_actor', '');
  const before = action === 'update' ? snapshot(e.record.original()) : null;
  e.next();
  const after = snapshot(e.record);
  let changes;
  if (action === 'create') changes = {after};
  else {
    changes = {before: {}, after: {}};
    for (const field in after) {
      if (UNAUDITED.includes(field) || JSON.stringify(before[field]) === JSON.stringify(after[field])) continue;
      changes.before[field] = before[field];
      changes.after[field] = after[field];
    }
    if (!Object.keys(changes.after).length) return;
  }
  const row = new Record(e.app.findCollectionByNameOrId('audit_log'));
  row.set('action', action); row.set('collection', e.record.collection().name); row.set('record', e.record.id);
  row.set('actor_type', who.split(':')[0]); row.set('actor', who.split(':')[1]); row.set('changes', changes);
  e.app.save(row);
}

module.exports = {KEYED, OWNED, AUDITED, recordId, validate, write: transactional, audit};
