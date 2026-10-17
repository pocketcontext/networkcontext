// NetworkContext schema: LinkedIn entities, per-account observations and ingestion runs.
// Ingested records use deterministic IDs derived from natural keys (see pb_hooks/ingest.js).
migrate((app) => {
  const access = "@request.auth.id != '' && @request.auth.collectionName = 'users'";
  let users;
  try { users = app.findCollectionByNameOrId('users'); } catch (_) { users = new Collection({type:'auth', name:'users'}); }
  users.listRule = null; users.viewRule = "id = @request.auth.id && @request.auth.collectionName = 'users'";
  users.createRule = null;
  users.updateRule = "id = @request.auth.id && @request.auth.collectionName = 'users'" +
    " && @request.body.email:isset = false && @request.body.name:isset = false" +
    " && @request.body.verified:isset = false && @request.body.emailVisibility:isset = false" +
    " && @request.body.disabled:isset = false";
  users.deleteRule = null;
  users.fields.removeByName('avatar');
  users.fields.add(new TextField({name:'name', required:true, max:200}));
  users.fields.add(new BoolField({name:'disabled'}));
  users.authRule = 'disabled = false';
  users.passwordAuth = {enabled:true, identityFields:['email']};
  users.authToken.duration = 604800; users.authAlert.enabled = false;
  app.save(users);

  const text = (name, required=false, max=500) => ({name,type:'text',required,max});
  const date = (name) => ({name,type:'date'});
  const num = (name) => ({name,type:'number',onlyInt:true,min:0});
  const bool = (name) => ({name,type:'bool'});
  const json = (name) => ({name,type:'json',maxSize:1048576});
  const url = (name) => ({name,type:'url'});
  const rel = (name, collection, required=false) => ({name,type:'relation',collectionId:app.findCollectionByNameOrId(collection).id,maxSelect:1,required,cascadeDelete:false});
  const select = (name, values, required=true) => ({name,type:'select',values,maxSelect:1,required});
  const precision = () => select('time_precision', ['exact','minute','day','approximate','unknown'], false);
  const times = () => [{name:'created',type:'autodate',onCreate:true}, {name:'updated',type:'autodate',onCreate:true,onUpdate:true}];
  const stamps = () => [rel('created_by','users'), rel('updated_by','users')].concat(times());
  // Fields shared by every collection written by ingestion. last_run is added once runs exist.
  const observed = () => [text('natural_key',true,1000), select('key_kind',['urn','url','composite','hash']),
    text('content_hash',false,64), date('first_seen_at'), date('last_seen_at')];
  function create(name, fields, indexes=[], writable=true) {
    app.save(new Collection({name, type:'base', listRule:access, viewRule:access,
      createRule:writable ? access : null, updateRule:writable ? access : null, deleteRule:null,
      fields:fields.concat(writable ? stamps() : times()), indexes}));
  }
  const keyed = (name) => [`CREATE UNIQUE INDEX idx_${name}_key ON ${name} (natural_key)`];

  // Safe display-name projection of users: no timestamps, nothing else to expose.
  app.save(new Collection({name:'user_directory', type:'base', listRule:access, viewRule:access, createRule:null, updateRule:null, deleteRule:null,
    fields:[text('name',true,200)]}));
  create('organizations', observed().concat([text('org_urn',false,200), text('universal_name',false,200), text('name',false,300),
    select('kind',['company','school','unknown'],false), text('industry',false,200), text('size_range',false,100),
    text('headquarters',false,300), url('url'), text('description',false,20000)]),
    keyed('organizations').concat(['CREATE INDEX idx_organizations_name ON organizations (name)']));
  create('profiles', observed().concat([text('member_urn',false,200), text('public_id',false,200), text('name',false,300),
    text('headline',false,1000), text('location',false,300), text('about',false,20000), url('profile_url'),
    select('connection_degree',['self','1st','2nd','3rd','out_of_network'],false), bool('is_self'),
    rel('current_organization','organizations')]),
    keyed('profiles').concat(['CREATE INDEX idx_profiles_name ON profiles (name)', 'CREATE INDEX idx_profiles_public_id ON profiles (public_id)']));
  const profiles = app.findCollectionByNameOrId('profiles');
  profiles.fields.add(new RelationField(rel('merged_into','profiles'))); app.save(profiles);
  create('aliases', [text('value',true,1000), select('kind',['urn','url','public_id']), rel('profile','profiles'), rel('organization','organizations')],
    ['CREATE UNIQUE INDEX idx_aliases_value ON aliases (value)', 'CREATE INDEX idx_aliases_profile ON aliases (profile)']);
  create('accounts', [rel('user','users',true), rel('profile','profiles',true), text('label',true,200)],
    ['CREATE UNIQUE INDEX idx_accounts_profile ON accounts (profile)', 'CREATE INDEX idx_accounts_user ON accounts (user)']);
  create('ingestion_runs', [rel('account','accounts',true),
    select('surface',['feed','notifications','network','profiles','conversations','messages','export']),
    date('since'), date('started_at'), date('completed_at'), select('status',['running','complete','failed']),
    bool('reached_cutoff'), json('counts'), text('capture_schema',false,100), text('agent',false,200), text('note',false,2000)],
    ['CREATE INDEX idx_runs_account ON ingestion_runs (account, surface, status)']);
  create('sync_state', [rel('account','accounts',true), select('surface',['feed','notifications','network','profiles','conversations','messages','export']),
    date('high_water_at'), rel('last_complete_run','ingestion_runs')],
    ['CREATE UNIQUE INDEX idx_sync_state ON sync_state (account, surface)'], false);
  for (const name of ['organizations','profiles']) {
    const c = app.findCollectionByNameOrId(name); c.fields.add(new RelationField(rel('last_run','ingestion_runs'))); app.save(c);
  }
  const ingested = (name, fields, indexes=[]) => create(name, observed().concat(fields, [rel('last_run','ingestion_runs')]), keyed(name).concat(indexes));

  ingested('positions', [rel('profile','profiles',true), rel('organization','organizations'), text('organization_name',false,300),
    text('title',false,300), text('start_month',false,7), text('end_month',false,7), bool('current'), text('location',false,300), text('description',false,20000)],
    ['CREATE INDEX idx_positions_profile ON positions (profile)', 'CREATE INDEX idx_positions_org ON positions (organization)']);
  ingested('education', [rel('profile','profiles',true), rel('organization','organizations'), text('school_name',false,300),
    text('degree',false,300), text('field',false,300), text('start_year',false,4), text('end_year',false,4)],
    ['CREATE INDEX idx_education_profile ON education (profile)']);
  ingested('posts', [text('post_urn',true,200), rel('author_profile','profiles'), rel('author_organization','organizations'),
    text('text',false,100000), date('posted_at'), precision(), url('url'), text('article_url',false,2000),
    select('media_kind',['none','image','video','document','article','poll','event','other'],false),
    num('reaction_count'), num('comment_count'), num('repost_count')],
    ['CREATE INDEX idx_posts_author ON posts (author_profile, posted_at)', 'CREATE INDEX idx_posts_org ON posts (author_organization, posted_at)', 'CREATE INDEX idx_posts_time ON posts (posted_at)']);
  const posts = app.findCollectionByNameOrId('posts'); posts.fields.add(new RelationField(rel('reshare_of','posts'))); app.save(posts);
  ingested('comments', [text('comment_urn',true,300), rel('post','posts',true), rel('author','profiles'),
    text('text',false,50000), date('posted_at'), precision()],
    ['CREATE INDEX idx_comments_post ON comments (post, posted_at)', 'CREATE INDEX idx_comments_author ON comments (author)']);
  const comments = app.findCollectionByNameOrId('comments'); comments.fields.add(new RelationField(rel('parent','comments'))); app.save(comments);
  ingested('reactions', [rel('actor','profiles',true), rel('post','posts'), rel('comment','comments'),
    select('kind',['like','celebrate','support','love','insightful','funny','other'])],
    ['CREATE INDEX idx_reactions_post ON reactions (post)', 'CREATE INDEX idx_reactions_actor ON reactions (actor)']);
  ingested('feed_items', [rel('account','accounts',true), rel('post','posts',true), date('seen_at'), num('position'),
    text('reason',false,500), rel('actor','profiles'), bool('promoted')],
    ['CREATE INDEX idx_feed_items_account ON feed_items (account, seen_at)', 'CREATE INDEX idx_feed_items_post ON feed_items (post)']);
  ingested('notifications', [rel('account','accounts',true), text('notification_urn',false,300), text('kind',false,100),
    text('text',false,5000), rel('actor','profiles'), rel('target_post','posts'), rel('target_profile','profiles'),
    date('occurred_at'), precision(), url('url')],
    ['CREATE INDEX idx_notifications_account ON notifications (account, occurred_at)']);
  ingested('connections', [rel('account','accounts',true), rel('profile','profiles',true),
    select('status',['connected','removed']), date('connected_at'), precision()],
    ['CREATE INDEX idx_connections_profile ON connections (profile)']);
  ingested('invitations', [rel('account','accounts',true), rel('profile','profiles',true), select('direction',['incoming','outgoing']),
    select('status',['pending','accepted','ignored','withdrawn']), text('message',false,2000), date('sent_at'), precision()],
    ['CREATE INDEX idx_invitations_profile ON invitations (profile)']);
  ingested('follows', [rel('account','accounts',true), rel('profile','profiles'), rel('organization','organizations'),
    select('direction',['following','follower'])], []);
  ingested('conversations', [rel('account','accounts',true), text('thread_urn',true,300), text('title',false,500),
    date('last_activity_at'), url('url')],
    ['CREATE INDEX idx_conversations_account ON conversations (account, last_activity_at)']);
  ingested('conversation_participants', [rel('conversation','conversations',true), rel('profile','profiles',true)],
    ['CREATE INDEX idx_participants_profile ON conversation_participants (profile)']);
  ingested('messages', [rel('conversation','conversations',true), rel('sender','profiles'), text('event_urn',false,300),
    text('body',false,100000), date('sent_at'), precision(), json('attachments')],
    ['CREATE INDEX idx_messages_conversation ON messages (conversation, sent_at)', 'CREATE INDEX idx_messages_sender ON messages (sender)']);
  create('crm_links', [rel('profile','profiles',true), select('app',['dealcontext','raisecontext','peoplecontext','other']),
    text('target_id',true,100), url('target_url'), text('note',false,2000)],
    ['CREATE UNIQUE INDEX idx_crm_links ON crm_links (profile, app, target_id)']);
  create('audit_log', [select('action',['create','update']), text('collection',true), text('record',true), text('actor'),
    select('actor_type',['user','superuser']), {name:'changes',type:'json',maxSize:5242880}],
    ['CREATE INDEX idx_audit_record ON audit_log (collection, record, created)'], false);

  // Search indexes in PocketContext's canonical FTS5 form, maintained by pb_hooks/search.js.
  app.db().newQuery(`CREATE VIRTUAL TABLE "profiles_search" USING fts5("record_id" UNINDEXED, "name", "headline", "about", tokenize='unicode61')`).execute();
  app.db().newQuery(`CREATE VIRTUAL TABLE "posts_search" USING fts5("record_id" UNINDEXED, "text", tokenize='unicode61')`).execute();
  app.db().newQuery(`CREATE VIRTUAL TABLE "messages_search" USING fts5("record_id" UNINDEXED, "body", tokenize='unicode61')`).execute();

  const settings = app.settings(); settings.batch.enabled = true; settings.batch.maxRequests = 100; settings.batch.timeout = 10; app.save(settings);
}, () => { throw new Error('Initial schema rollback requires a deliberate backup restore.'); });
