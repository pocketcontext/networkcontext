"""Turn capture envelopes (networkcontext.capture/v1) into idempotent record writes.

Each envelope becomes entities keyed by natural keys. Profiles and organizations
resolve through `aliases`, so a profile first seen by its URL keeps its record
when its URN appears later. Writes go through PocketBase batch upserts in
dependency order; unchanged records only advance their observation fields.
"""
from datetime import datetime, timedelta, timezone
import json

from . import keys
from .keys import Invalid, clean, content_hash, db_time, record_id, verbatim

SCHEMA = 'networkcontext.capture/v1'
KINDS = ('profile', 'organization', 'post', 'comment', 'reaction', 'notification', 'connection',
         'invitation', 'follow', 'conversation', 'message')
SURFACE_KINDS = {
    'feed': {'post', 'comment', 'reaction', 'profile', 'organization'},
    'notifications': {'notification', 'post', 'profile', 'organization'},
    'network': {'connection', 'invitation', 'follow', 'profile', 'organization'},
    'profiles': {'profile', 'organization', 'post', 'comment', 'reaction'},
    'conversations': {'conversation', 'message', 'profile'},
    'messages': {'conversation', 'message', 'profile'},
    'export': set(KINDS),
}
# Lower tiers are written first: relation targets always exist before their referrers.
TIER = {'organizations': 0, 'profiles': 1, 'aliases': 2, 'posts': 3, 'conversations': 3, 'connections': 4, 'invitations': 4,
        'follows': 4, 'positions': 4, 'education': 4, 'conversation_participants': 4, 'messages': 5, 'comments': 5,
        'feed_items': 6, 'notifications': 6, 'reactions': 7}
COUNTERS = ('reaction_count', 'comment_count', 'repost_count')
BATCH = 50
REACTIONS = ('like', 'celebrate', 'support', 'love', 'insightful', 'funny')
MEDIA = ('none', 'image', 'video', 'document', 'article', 'poll', 'event', 'other')


class Entity:
    """One record to upsert. `ref` entities only ensure existence and never overwrite captured fields."""

    def __init__(self, collection, key, kind, fields, ref=False, seen=None, aliases=(), primary=False):
        self.collection, self.key, self.kind, self.fields, self.ref = collection, key, kind, dict(fields), ref
        self.seen, self.aliases, self.primary = seen, list(aliases), primary
        self.id = record_id(collection, key)
        # Stands for this entity's final record id inside composite keys of other entities.
        self.token = '{' + collection + '/' + key + '}'
        self.deps = []  # (field, Entity) relations resolved to ids before writing

    def link(self, field, target):
        if target is not None:
            self.deps.append((field, target))
        return target


class Plan:
    def __init__(self, account, run, observed_default=None):
        self.account, self.run = account, run
        self.entities = {}
        self.primaries = []
        self.rejects = []
        self.observed_default = observed_default

    # Entity registration, merging duplicates within one plan.
    def add(self, entity):
        known = self.entities.get((entity.collection, entity.key))
        if known is None:
            self.entities[(entity.collection, entity.key)] = entity
            return entity
        known.aliases.extend(a for a in entity.aliases if a not in known.aliases)
        if known.seen is None or (entity.seen and entity.seen > known.seen):
            known.seen = entity.seen
        if not entity.ref:
            if known.ref:
                known.fields, known.ref = dict(entity.fields), False
            else:
                known.fields.update({k: v for k, v in entity.fields.items() if v not in ('', None) or k not in known.fields})
            known.deps.extend(entity.deps)
        known.primary = known.primary or entity.primary
        return known

    # References
    def profile(self, value, seen, full=None):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise Invalid('profile references must be objects')
        urn = keys.profile_urn(value.get('urn')) if value.get('urn') else None
        if value.get('urn') and not urn:
            raise Invalid(f'unrecognized profile URN: {value.get("urn")!r}')
        url, slug = keys.profile_url(value['url']) if value.get('url') else (None, None)
        if value.get('public_id') and not slug:
            url, slug = keys.profile_url(f'{keys.LINKEDIN}/in/{value["public_id"]}/')
        if not urn and not url:
            raise Invalid('a profile needs a LinkedIn URN or profile URL')
        aliases = [('urn', urn)] if urn else []
        if url:
            aliases += [('url', url)]
        # Identity fields are only ever added, so a capture without a URN never erases a known one.
        fields = {k: v for k, v in (('member_urn', urn), ('public_id', slug), ('profile_url', url)) if v}
        for name, limit in (('name', 300), ('headline', 1000)):
            if value.get(name):
                fields[name] = verbatim(value[name], limit)
        if full is not None:
            fields.update(full)
        entity = Entity('profiles', urn or url, 'urn' if urn else 'url', fields, ref=full is None, seen=seen, aliases=aliases)
        return self.add(entity)

    def organization(self, value, seen, full=None):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise Invalid('organization references must be objects')
        urn = keys.organization_urn(value.get('urn')) if value.get('urn') else None
        if value.get('urn') and not urn:
            raise Invalid(f'unrecognized organization URN: {value.get("urn")!r}')
        url, slug, kind = keys.organization_url(value['url']) if value.get('url') else (None, None, None)
        if value.get('universal_name') and not url:
            url, slug, kind = keys.organization_url(f'{keys.LINKEDIN}/company/{value["universal_name"]}/')
        if not urn and not url:
            return None  # A name alone is kept as text by the referrer.
        fields = {k: v for k, v in (('org_urn', urn), ('universal_name', slug), ('url', url)) if v}
        if value.get('name'):
            fields['name'] = verbatim(value['name'], 300)
        if kind or (urn and ':school:' in urn):
            fields['kind'] = 'school' if (kind == 'school' or (urn and ':school:' in urn)) else 'company'
        if full is not None:
            fields.update(full)
        aliases = ([('urn', urn)] if urn else []) + ([('url', url)] if url else [])
        return self.add(Entity('organizations', urn or url, 'urn' if urn else 'url', fields, ref=full is None, seen=seen, aliases=aliases))

    def post_ref(self, urn, seen):
        canonical = keys.post_urn(urn)
        if not canonical:
            raise Invalid(f'unrecognized post URN: {urn!r}')
        fields = {'post_urn': canonical}
        moment = keys.snowflake_time(canonical)
        if moment:
            fields.update(posted_at=db_time(moment), time_precision='exact')
        return self.add(Entity('posts', canonical, 'urn', fields, ref=True, seen=seen))

    def owned(self, collection, parts, kind, fields, seen, primary=True):
        key = self.account + '|' + '|'.join(parts)
        entity = Entity(collection, key, kind, dict(fields, account=self.account), seen=seen, primary=primary)
        return entity

    # Envelopes
    def envelope(self, line_number, envelope, surface):
        try:
            if not isinstance(envelope, dict):
                raise Invalid('an envelope must be a JSON object')
            if envelope.get('schema') != SCHEMA:
                raise Invalid(f'unsupported schema {envelope.get("schema")!r}; expected {SCHEMA}')
            kind = envelope.get('kind')
            if kind not in KINDS:
                raise Invalid(f'unknown kind {kind!r}')
            if kind not in SURFACE_KINDS[surface]:
                raise Invalid(f'kind {kind} does not belong to surface {surface}')
            item = envelope.get('item')
            if not isinstance(item, dict):
                raise Invalid('item must be an object')
            observed = keys.parse_instant(envelope['observed_at']) if envelope.get('observed_at') else self.observed_default
            if observed is None:
                raise Invalid('observed_at is required')
            if observed > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise Invalid('observed_at is in the future')
            before = dict(self.entities)
            primary = getattr(self, 'kind_' + kind)(item, observed)
            primary.primary = True
            self.primaries.append(primary)
        except (Invalid, KeyError, TypeError, ValueError) as error:
            self.entities = before if 'before' in locals() else self.entities
            self.rejects.append({'line': line_number, 'reason': str(error) or type(error).__name__})

    def kind_profile(self, item, seen):
        full = {'name': verbatim(item.get('name'), 300), 'headline': verbatim(item.get('headline'), 1000),
                'location': verbatim(item.get('location'), 300), 'about': verbatim(item.get('about'), 20000)}
        degree = item.get('connection_degree')
        if degree:
            if degree not in ('self', '1st', '2nd', '3rd', 'out_of_network'):
                raise Invalid('connection_degree must be self, 1st, 2nd, 3rd or out_of_network')
            full['connection_degree'] = degree
        current = self.organization(item.get('current_organization'), seen)
        profile = self.profile(item, seen, full=full)
        if current is not None:
            profile.link('current_organization', current)
        for position in item.get('positions') or []:
            org = self.organization(position.get('organization'), seen)
            name = verbatim((position.get('organization') or {}).get('name'), 300)
            start = month(position.get('start'))
            fields = {'organization_name': name, 'title': verbatim(position.get('title'), 300), 'start_month': start,
                      'end_month': month(position.get('end')), 'current': bool(position.get('current')) or not position.get('end'),
                      'location': verbatim(position.get('location'), 300), 'description': verbatim(position.get('description'), 20000)}
            entity = self.add(Entity('positions', '|'.join([profile.token, org.token if org else clean(name).lower(), clean(fields['title']).lower(), start]),
                                     'composite', fields, seen=seen))
            entity.link('profile', profile)
            entity.link('organization', org)
        for school in item.get('education') or []:
            org = self.organization(school.get('organization'), seen)
            name = verbatim((school.get('organization') or {}).get('name'), 300)
            fields = {'school_name': name, 'degree': verbatim(school.get('degree'), 300), 'field': verbatim(school.get('field'), 300),
                      'start_year': year(school.get('start_year')), 'end_year': year(school.get('end_year'))}
            entity = self.add(Entity('education', '|'.join([profile.token, org.token if org else clean(name).lower(), clean(fields['degree']).lower(), fields['start_year']]),
                                     'composite', fields, seen=seen))
            entity.link('profile', profile)
            entity.link('organization', org)
        return profile

    def kind_organization(self, item, seen):
        kind = item.get('kind')
        if kind and kind not in ('company', 'school'):
            raise Invalid('organization kind must be company or school')
        full = {'name': verbatim(item.get('name'), 300), 'industry': verbatim(item.get('industry'), 200),
                'size_range': verbatim(item.get('size_range'), 100), 'headquarters': verbatim(item.get('headquarters'), 300),
                'description': verbatim(item.get('description'), 20000)}
        if kind:
            full['kind'] = kind
        org = self.organization(item, seen, full=full)
        if org is None:
            raise Invalid('an organization needs a LinkedIn URN or company/school URL')
        return org

    def kind_post(self, item, seen):
        urn = keys.post_urn(item.get('urn') or item.get('url') or '')
        if not urn:
            raise Invalid('a post needs urn:li:activity, ugcPost or share')
        moment, precision = keys.item_time(item.get('posted_at'), seen, item.get('time_precision'))
        decoded = keys.snowflake_time(urn)
        if decoded:
            moment, precision = decoded, 'exact'
        media = item.get('media_kind') or 'none'
        if media not in MEDIA:
            raise Invalid('media_kind must be one of ' + ', '.join(MEDIA))
        counts = item.get('counts') or {}
        fields = {'post_urn': urn, 'text': verbatim(item.get('text'), 100000), 'posted_at': db_time(moment),
                  'time_precision': precision, 'url': f'{keys.LINKEDIN}/feed/update/{urn}/',
                  'article_url': verbatim(item.get('article_url'), 2000), 'media_kind': media}
        for name in COUNTERS:
            value = counts.get(name.replace('_count', 's'))
            if value is not None:
                if not isinstance(value, int) or value < 0:
                    raise Invalid(f'counts.{name.replace("_count", "s")} must be a non-negative integer')
                fields[name] = value
        if item.get('author_profile') and item.get('author_organization'):
            raise Invalid('a post has one author')
        post = self.add(Entity('posts', urn, 'urn', fields, seen=seen))
        post.link('author_profile', self.profile(item.get('author_profile'), seen))
        post.link('author_organization', self.organization(item.get('author_organization'), seen))
        if item.get('reshare_of'):
            # An embedded original is a partial view: it can create the post but never overwrites a capture of it.
            embedded = item['reshare_of'] if isinstance(item['reshare_of'], dict) else {'urn': item['reshare_of']}
            original = self.post_ref(embedded.get('urn') or embedded.get('url') or '', seen)
            if original.ref and embedded.get('text'):
                original.fields['text'] = verbatim(embedded['text'], 100000)
            if original.ref:
                original.link('author_profile', self.profile(embedded.get('author_profile'), seen))
                original.link('author_organization', self.organization(embedded.get('author_organization'), seen))
            post.link('reshare_of', original)
        feed = item.get('feed')
        if feed is not None:
            if not isinstance(feed, dict):
                raise Invalid('feed must be an object')
            position = feed.get('position')
            if position is not None and (not isinstance(position, int) or position < 0):
                raise Invalid('feed.position must be a non-negative integer')
            reason = verbatim(feed.get('reason'), 500)
            entity = self.add(self.owned('feed_items', [post.key, clean(reason).lower(), seen.astimezone(timezone.utc).date().isoformat()], 'composite',
                                         {'seen_at': db_time(seen), 'position': position or 0, 'reason': reason, 'promoted': bool(feed.get('promoted'))}, seen))
            entity.link('post', post)
            entity.link('actor', self.profile(feed.get('actor'), seen))
        return post

    def kind_comment(self, item, seen):
        urn, post_urn = keys.comment_urn(item.get('urn') or '')
        if not urn:
            raise Invalid('a comment needs urn:li:comment:(<post>,<id>)')
        moment, precision = keys.item_time(item.get('posted_at'), seen, item.get('time_precision'))
        decoded = keys.snowflake_time(urn)
        if decoded:
            moment, precision = decoded, 'exact'
        comment = self.add(Entity('comments', urn, 'urn', {'comment_urn': urn, 'text': verbatim(item.get('text'), 50000),
                                                           'posted_at': db_time(moment), 'time_precision': precision}, seen=seen))
        comment.link('post', self.post_ref(item.get('post_urn') or post_urn, seen))
        comment.link('author', self.profile(item.get('author'), seen))
        if item.get('parent_urn'):
            parent, _ = keys.comment_urn(item['parent_urn'])
            if not parent:
                raise Invalid('parent_urn is not a comment URN')
            ref = self.add(Entity('comments', parent, 'urn', {'comment_urn': parent}, ref=True, seen=seen))
            ref.link('post', comment.deps[0][1])
            comment.link('parent', ref)
        return comment

    def kind_reaction(self, item, seen):
        kind = item.get('kind') or 'like'
        actor = self.profile(item.get('actor'), seen)
        if actor is None:
            raise Invalid('a reaction needs an actor')
        target = item.get('target_urn') or ''
        comment_urn, post_urn = keys.comment_urn(target)
        if comment_urn:
            ref = self.add(Entity('comments', comment_urn, 'urn', {'comment_urn': comment_urn}, ref=True, seen=seen))
            ref.link('post', self.post_ref(post_urn, seen))
            field = 'comment'
        else:
            ref, field = self.post_ref(target, seen), 'post'
        entity = self.add(Entity('reactions', actor.token + '|' + ref.token, 'composite',
                                 {'kind': kind if kind in REACTIONS else 'other'}, seen=seen))
        entity.link('actor', actor)
        entity.link(field, ref)
        return entity

    def kind_notification(self, item, seen):
        moment, precision = keys.item_time(item.get('occurred_at'), seen, item.get('time_precision'))
        text = verbatim(item.get('text'), 5000)
        kind = clean(item.get('kind'), 100)
        if not text and not kind:
            raise Invalid('a notification needs kind or text')
        urn = clean(item.get('urn'), 300)
        target_post = self.post_ref(item['target_post_urn'], seen) if item.get('target_post_urn') else None
        target_profile = self.profile(item.get('target_profile'), seen)
        day = (moment or seen).astimezone(timezone.utc).date().isoformat()
        if urn:
            parts = [urn]
        else:
            parts = [content_hash([kind, clean(text).lower(), day])[:32], target_post.token if target_post else '', target_profile.token if target_profile else '']
        entity = self.add(self.owned('notifications', parts, 'urn' if urn else 'hash',
                                     {'notification_urn': urn, 'kind': kind, 'text': text, 'occurred_at': db_time(moment),
                                      'time_precision': precision, 'url': item.get('url') or ''}, seen))
        entity.link('actor', self.profile(item.get('actor'), seen))
        entity.link('target_post', target_post)
        entity.link('target_profile', target_profile)
        return entity

    def kind_connection(self, item, seen):
        status = item.get('status') or 'connected'
        if status not in ('connected', 'removed'):
            raise Invalid('connection status must be connected or removed')
        profile = self.profile(item.get('profile'), seen)
        if profile is None:
            raise Invalid('a connection needs a profile')
        moment, precision = keys.item_time(item.get('connected_at'), seen, item.get('time_precision'))
        entity = self.add(self.owned('connections', [profile.token], 'composite',
                                     {'status': status, 'connected_at': db_time(moment), 'time_precision': precision}, seen))
        entity.link('profile', profile)
        return entity

    def kind_invitation(self, item, seen):
        direction, status = item.get('direction'), item.get('status') or 'pending'
        if direction not in ('incoming', 'outgoing'):
            raise Invalid('invitation direction must be incoming or outgoing')
        if status not in ('pending', 'accepted', 'ignored', 'withdrawn'):
            raise Invalid('invitation status must be pending, accepted, ignored or withdrawn')
        profile = self.profile(item.get('profile'), seen)
        if profile is None:
            raise Invalid('an invitation needs a profile')
        moment, precision = keys.item_time(item.get('sent_at'), seen, item.get('time_precision'))
        entity = self.add(self.owned('invitations', [profile.token, direction], 'composite',
                                     {'direction': direction, 'status': status, 'message': verbatim(item.get('message'), 2000),
                                      'sent_at': db_time(moment), 'time_precision': precision}, seen))
        entity.link('profile', profile)
        return entity

    def kind_follow(self, item, seen):
        direction = item.get('direction') or 'following'
        if direction not in ('following', 'follower'):
            raise Invalid('follow direction must be following or follower')
        profile = self.profile(item.get('profile'), seen)
        org = self.organization(item.get('organization'), seen)
        if (profile is None) == (org is None):
            raise Invalid('a follow needs exactly one of profile and organization (with URN or URL)')
        entity = self.add(self.owned('follows', [profile.token if profile else org.token, direction], 'composite', {'direction': direction}, seen))
        entity.link('profile', profile)
        entity.link('organization', org)
        return entity

    def conversation(self, item, seen, full):
        key, url = keys.thread_key(item.get('thread_urn'), item.get('url'))
        if not key:
            raise Invalid('a conversation needs a thread URN or messaging thread URL')
        fields = {'thread_urn': key, 'url': url}
        if full:
            moment, _ = keys.item_time(item.get('last_activity_at'), seen)
            fields.update(title=verbatim(item.get('title'), 500), last_activity_at=db_time(moment))
        entity = self.owned('conversations', [key], 'urn', fields, seen, primary=full)
        entity.ref = not full
        return self.add(entity)

    def kind_conversation(self, item, seen):
        conversation = self.conversation(item, seen, True)
        for participant in item.get('participants') or []:
            profile = self.profile(participant, seen)
            entity = self.add(Entity('conversation_participants', conversation.token + '|' + profile.token, 'composite', {}, seen=seen))
            entity.link('conversation', conversation)
            entity.link('profile', profile)
        return conversation

    def kind_message(self, item, seen):
        body = verbatim(item.get('body'), 100000)
        attachments = item.get('attachments') or []
        if not isinstance(attachments, list) or not all(isinstance(a, dict) for a in attachments):
            raise Invalid('attachments must be a list of objects')
        attachments = [{'name': verbatim(a.get('name'), 300), 'url': verbatim(a.get('url'), 2000)} for a in attachments]
        if not body and not attachments:
            raise Invalid('a message needs a body or attachments')
        moment, precision = keys.item_time(item.get('sent_at'), seen, item.get('time_precision'))
        conversation = self.conversation(item, seen, False)
        sender = self.profile(item.get('sender'), seen)
        event = clean(item.get('event_urn'), 300)
        if event:
            parts, kind = [conversation.token, event], 'urn'
        else:
            minute = db_time(moment)[:16] if moment else ''
            parts, kind = [conversation.token, sender.token if sender else '', content_hash([minute, clean(body)])[:32]], 'hash'
        entity = self.add(Entity('messages', '|'.join(parts), kind, {'event_urn': event, 'body': body, 'sent_at': db_time(moment),
                                                                     'time_precision': precision, 'attachments': attachments}, seen=seen))
        entity.link('conversation', conversation)
        entity.link('sender', sender)
        if sender is not None:
            participant = self.add(Entity('conversation_participants', conversation.token + '|' + sender.token, 'composite', {}, seen=seen))
            participant.link('conversation', conversation)
            participant.link('profile', sender)
        return entity


def month(value):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if len(text) == 4 and text.isdigit():
        return text
    if len(text) >= 7 and text[:4].isdigit() and text[4] == '-' and text[5:7].isdigit():
        return text[:7]
    raise Invalid(f'months use YYYY or YYYY-MM, not {value!r}')


def year(value):
    if value in (None, ''):
        return ''
    text = str(value).strip()[:4]
    if not text.isdigit():
        raise Invalid(f'years use YYYY, not {value!r}')
    return text


class Writer:
    """Resolves identities against the server and writes a Plan."""

    def __init__(self, client):
        self.client = client

    def sql_list(self, values):
        return ','.join("'" + value.replace("'", "''") + "'" for value in values)

    def rows(self, sql):
        return self.client.query(sql)

    def resolve(self, plan):
        """Point profiles and organizations at records that already own one of their aliases."""
        for collection in ('profiles', 'organizations'):
            entities = [e for e in plan.entities.values() if e.collection == collection]
            values = sorted({value for e in entities for _, value in e.aliases})
            found = {}
            for chunk in chunks(values, 200):
                field = 'profile' if collection == 'profiles' else 'organization'
                for row in self.rows(f"SELECT a.value, a.{field} AS record, t.natural_key FROM aliases a JOIN {collection} t ON t.id = a.{field} WHERE a.value IN ({self.sql_list(chunk)})"):
                    found[row['value']] = (row['record'], row['natural_key'])
            for entity in entities:
                owners = {found[value] for _, value in entity.aliases if value in found}
                if len(owners) > 1:
                    plan.rejects.append({'line': None, 'reason': f'{collection} identifiers {entity.aliases} belong to several records {sorted(o[0] for o in owners)}; merge them first'})
                    owners = {sorted(owners)[0]}
                if owners:
                    entity.id, entity.key = next(iter(owners))
                entity.new_aliases = [(kind, value) for kind, value in entity.aliases if value not in found]

    def finalize_keys(self, plan):
        """Substitute resolved relation ids into composite keys, then derive ids."""
        for entity in sorted(plan.entities.values(), key=lambda e: TIER[e.collection]):
            if '{' in entity.key:
                key = entity.key
                for _, target in entity.deps:
                    key = key.replace(target.token, target.id)
                entity.key = key
                entity.id = record_id(entity.collection, key)
        # Merge entities that became identical once relation ids were known.
        merged = {}
        for entity in plan.entities.values():
            other = merged.get((entity.collection, entity.id))
            if other is None:
                merged[(entity.collection, entity.id)] = entity
            else:
                other.new_aliases = getattr(other, 'new_aliases', []) + [a for a in getattr(entity, 'new_aliases', []) if a not in getattr(other, 'new_aliases', [])]
                if not entity.ref:
                    other.fields.update(entity.fields)
                    other.deps.extend(entity.deps)
                    other.ref = False
                other.primary = other.primary or entity.primary
        plan.unique = list(merged.values())

    def existing(self, entities):
        found = {}
        by_collection = {}
        for entity in entities:
            by_collection.setdefault(entity.collection, []).append(entity.id)
        for collection, ids in by_collection.items():
            for chunk in chunks(sorted(set(ids)), 200):
                for row in self.rows(f'SELECT id, content_hash, first_seen_at, last_seen_at FROM {collection} WHERE id IN ({self.sql_list(chunk)})'):
                    found[(collection, row['id'])] = row
        return found

    def requests(self, plan):
        self.resolve(plan)
        self.finalize_keys(plan)
        known = self.existing(plan.unique)
        outcome = {}
        operations = []
        aliases = set()
        for entity in sorted(plan.unique, key=lambda e: (TIER[e.collection], depth(e))):
            seen = db_time(entity.seen) if entity.seen else ''
            fields = dict(entity.fields)
            for field, target in entity.deps:
                fields[field] = target.id
            row = known.get((entity.collection, entity.id))
            touch = {'last_seen_at': seen, 'last_run': plan.run}
            path = f'/api/collections/{entity.collection}/records'
            if entity.ref:
                if row:
                    body, state = touch, 'unchanged'
                else:
                    body, state = dict(fields, id=entity.id, natural_key=entity.key, key_kind=entity.kind, first_seen_at=seen, **touch), 'created'
            else:
                digest = content_hash({k: v for k, v in fields.items() if k not in COUNTERS})
                if row and row['content_hash'] == digest:
                    body = dict(touch, **{k: fields[k] for k in COUNTERS if k in fields})
                    state = 'unchanged'
                else:
                    body = dict(fields, id=entity.id, natural_key=entity.key, key_kind=entity.kind, content_hash=digest, first_seen_at=seen, **touch)
                    state = 'updated' if row else 'created'
            if row and 'id' in body:
                method, url = 'PUT', path
            elif row:
                method, url = 'PATCH', f'{path}/{entity.id}'
            else:
                method, url = 'PUT', path
            operations.append((entity, {'method': method, 'url': url, 'body': body}))
            outcome[(entity.collection, entity.id)] = state
            for kind, value in getattr(entity, 'new_aliases', []):
                if value not in aliases:
                    aliases.add(value)
                    field = 'profile' if entity.collection == 'profiles' else 'organization'
                    operations.append((None, {'method': 'POST', 'url': '/api/collections/aliases/records', 'body': {'value': value, 'kind': kind, field: entity.id}}))
        return operations, outcome

    def write(self, plan):
        operations, outcome = self.requests(plan)
        counts = {'seen': len(plan.primaries), 'created': 0, 'updated': 0, 'unchanged': 0, 'rejected': len(plan.rejects), 'records': {}}
        failed = set()
        for chunk in chunks(operations, BATCH):
            status, data = self.client.batch([op for _, op in chunk])
            if status < 400:
                continue
            # Isolate the failing write: replay the batch one request at a time.
            for entity, op in chunk:
                status, data = self.client.batch([op])
                if status >= 400:
                    label = f'{entity.collection} {entity.key}' if entity else f'alias {op["body"].get("value")}'
                    plan.rejects.append({'line': None, 'reason': f'{label}: {batch_message(data)}'})
                    if entity:
                        failed.add((entity.collection, entity.id))
        for (collection, record), state in outcome.items():
            if (collection, record) in failed:
                continue
            counts['records'].setdefault(collection, {'created': 0, 'updated': 0, 'unchanged': 0})[state] += 1
        primaries = {(e.collection, e.id) for e in plan.primaries}
        for key in primaries:
            if key in failed:
                continue
            counts[outcome.get(key, 'unchanged')] += 1
        counts['rejected'] = len(plan.rejects)
        return counts


def depth(entity, seen=()):
    """Within one collection (reshares, comment replies), write the referenced record first."""
    same = [t for _, t in entity.deps if t.collection == entity.collection and t is not entity and t not in seen]
    return 1 + max((depth(t, seen + (entity,)) for t in same), default=0) if same else 0


def batch_message(data):
    try:
        for entry in data['data']['requests'].values():
            response = entry['response']
            details = [f'{name}: {detail.get("message")}' for name, detail in (response.get('data') or {}).items() if isinstance(detail, dict)]
            return ' '.join([response.get('message') or ''] + details).strip()
    except (AttributeError, KeyError, TypeError):
        pass
    return json.dumps(data)[:500] if not isinstance(data, str) else data[:500]


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]
