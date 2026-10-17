export interface Entity {
  table: string;
  label: string;
  title: string[];
  subtitle?: string[];
  search: string[];
  filters?: Record<string, string[]>;
  relations?: Record<string, string>;
  hidden?: string[];
  markdown?: string[];
  menu?: boolean;
  relationLabels?: Record<string, string>;
  reverseLabels?: Record<string, string>;
  order?: [string, "ASC" | "DESC"];
}
export const app: {
  name: string;
  authCollection: string;
  google?: boolean;
  entities: Entity[];
} = {
  name: "NetworkContext",
  authCollection: "users",
  entities: [
    {
      table: "profiles",
      label: "People",
      title: ["name"],
      subtitle: ["headline", "connection_degree"],
      search: ["name", "headline", "location", "public_id", "about"],
      filters: { connection_degree: ["self", "1st", "2nd", "3rd", "out_of_network"] },
      relations: {
        current_organization: "organizations",
        merged_into: "profiles",
        last_run: "ingestion_runs",
      },
      hidden: ["content_hash"],
      relationLabels: { merged_into: "Merged into" },
      reverseLabels: { merged_into: "Merged profiles" },
    },
    {
      table: "posts",
      order: ["posted_at", "DESC"],
      label: "Posts",
      title: ["text"],
      subtitle: ["posted_at", "media_kind"],
      search: ["text", "post_urn", "article_url"],
      filters: { media_kind: ["none", "image", "video", "document", "article", "poll", "event", "other"] },
      relations: {
        author_profile: "profiles",
        author_organization: "organizations",
        reshare_of: "posts",
        last_run: "ingestion_runs",
      },
      hidden: ["content_hash"],
      relationLabels: { reshare_of: "Reshare of" },
      reverseLabels: { reshare_of: "Reshares", author_profile: "Posts", author_organization: "Posts" },
    },
    {
      table: "conversations",
      order: ["last_activity_at", "DESC"],
      label: "Conversations",
      title: ["title"],
      subtitle: ["last_activity_at"],
      search: ["title", "thread_urn"],
      relations: { account: "accounts", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
    },
    {
      table: "messages",
      order: ["sent_at", "DESC"],
      label: "Messages",
      title: ["body"],
      subtitle: ["sent_at"],
      search: ["body"],
      relations: { conversation: "conversations", sender: "profiles", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      reverseLabels: { conversation: "Messages", sender: "Messages sent" },
    },
    {
      table: "notifications",
      order: ["occurred_at", "DESC"],
      label: "Notifications",
      title: ["text"],
      subtitle: ["kind", "occurred_at"],
      search: ["text", "kind"],
      relations: {
        account: "accounts",
        actor: "profiles",
        target_post: "posts",
        target_profile: "profiles",
        last_run: "ingestion_runs",
      },
      hidden: ["content_hash"],
      relationLabels: { target_post: "Post", target_profile: "Profile" },
      reverseLabels: { actor: "Notifications as actor", target_post: "Notifications", target_profile: "Notifications about" },
    },
    {
      table: "feed_items",
      order: ["seen_at", "DESC"],
      label: "Feed sightings",
      title: ["seen_at", "reason"],
      subtitle: ["position", "promoted"],
      search: ["reason"],
      filters: { promoted: ["0", "1"] },
      relations: { account: "accounts", post: "posts", actor: "profiles", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      reverseLabels: { post: "Seen in feed", actor: "Feed activity" },
    },
    {
      table: "organizations",
      label: "Organizations",
      title: ["name"],
      subtitle: ["kind", "industry"],
      search: ["name", "universal_name", "industry", "headquarters"],
      filters: { kind: ["company", "school", "unknown"] },
      relations: { last_run: "ingestion_runs" },
      hidden: ["content_hash"],
    },
    {
      table: "connections",
      label: "Connections",
      title: ["status", "connected_at"],
      search: ["status"],
      filters: { status: ["connected", "removed"] },
      relations: { account: "accounts", profile: "profiles", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      reverseLabels: { profile: "Connection" },
    },
    {
      table: "invitations",
      label: "Invitations",
      title: ["direction", "status"],
      subtitle: ["sent_at"],
      search: ["message"],
      filters: {
        direction: ["incoming", "outgoing"],
        status: ["pending", "accepted", "ignored", "withdrawn"],
      },
      relations: { account: "accounts", profile: "profiles", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
    },
    {
      table: "comments",
      order: ["posted_at", "DESC"],
      label: "Comments",
      title: ["text"],
      subtitle: ["posted_at"],
      search: ["text", "comment_urn"],
      relations: { post: "posts", parent: "comments", author: "profiles", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      relationLabels: { parent: "Reply to" },
      reverseLabels: { parent: "Replies", author: "Comments written" },
    },
    {
      table: "positions",
      label: "Positions",
      title: ["title", "organization_name"],
      subtitle: ["start_month", "end_month"],
      search: ["title", "organization_name", "location"],
      filters: { current: ["1", "0"] },
      relations: { profile: "profiles", organization: "organizations", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
    },
    {
      table: "education",
      label: "Education",
      title: ["school_name", "degree"],
      search: ["school_name", "degree", "field"],
      relations: { profile: "profiles", organization: "organizations", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      menu: false,
    },
    {
      table: "reactions",
      label: "Reactions",
      title: ["kind"],
      search: ["kind"],
      filters: { kind: ["like", "celebrate", "support", "love", "insightful", "funny", "other"] },
      relations: { actor: "profiles", post: "posts", comment: "comments", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      reverseLabels: { actor: "Reactions given" },
      menu: false,
    },
    {
      table: "follows",
      label: "Follows",
      title: ["direction"],
      search: ["direction"],
      filters: { direction: ["following", "follower"] },
      relations: { account: "accounts", profile: "profiles", organization: "organizations", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      menu: false,
    },
    {
      table: "conversation_participants",
      label: "Participants",
      title: ["id"],
      search: [],
      relations: { conversation: "conversations", profile: "profiles", last_run: "ingestion_runs" },
      hidden: ["content_hash"],
      reverseLabels: { conversation: "Participants", profile: "Conversations" },
      menu: false,
    },
    {
      table: "crm_links",
      label: "CRM links",
      title: ["app", "target_id"],
      search: ["app", "target_id", "note"],
      relations: { profile: "profiles" },
    },
    {
      table: "ingestion_runs",
      order: ["started_at", "DESC"],
      label: "Runs",
      title: ["surface", "started_at"],
      subtitle: ["status"],
      search: ["surface", "agent", "note"],
      filters: {
        surface: ["feed", "notifications", "network", "profiles", "conversations", "messages", "export"],
        status: ["running", "complete", "failed"],
      },
      relations: { account: "accounts" },
    },
    {
      table: "accounts",
      label: "Accounts",
      title: ["label"],
      search: ["label"],
      relations: { profile: "profiles", user: "user_directory" },
      menu: false,
    },
    {
      table: "aliases",
      label: "Aliases",
      title: ["value"],
      subtitle: ["kind"],
      search: ["value"],
      relations: { profile: "profiles", organization: "organizations" },
      reverseLabels: { profile: "Identifiers", organization: "Identifiers" },
      menu: false,
    },
    {
      table: "sync_state",
      label: "Sync state",
      title: ["surface", "high_water_at"],
      search: ["surface"],
      relations: { account: "accounts", last_complete_run: "ingestion_runs" },
      menu: false,
    },
    {
      table: "user_directory",
      label: "Users",
      title: ["name"],
      search: ["name"],
      menu: false,
    },
  ],
};
for (const e of app.entities)
  if (!["user_directory", "sync_state"].includes(e.table))
    e.relations = {
      ...e.relations,
      created_by: "user_directory",
      updated_by: "user_directory",
    };
