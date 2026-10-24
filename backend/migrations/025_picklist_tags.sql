-- Every order on a pick list gets the Shopify tag "picklist <quotation number>"
-- when the list is created; cancelling the list removes it again. The tag is
-- kept on the pick list so cancel removes exactly what was added, and each
-- order records how its last tag write went so failures can be shown and retried.
-- tag_state NULL = never tagged (pick lists from before this migration).
ALTER TABLE picklists ADD COLUMN IF NOT EXISTS shopify_tag TEXT;
ALTER TABLE picklist_orders ADD COLUMN IF NOT EXISTS tag_state TEXT
  CHECK (tag_state IN ('pending', 'tagged', 'failed', 'removed', 'remove_failed'));
ALTER TABLE picklist_orders ADD COLUMN IF NOT EXISTS tag_error TEXT;
