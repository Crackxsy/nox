-- 0018_node_opt_in: groups the owner switched on for a node on the core itself (C9).
--
-- Some groups need two keys: the device enables them (`nox node allow voice`) and the owner
-- switches them on in the Devices page as well, with the PIN (`nox.node.policy.CORE_OPT_IN`).
-- `core_opted_in` records the second key. Without it such a group stays narrowed on the core,
-- whatever the device reports and whenever it reports it; removing the node clears it.

ALTER TABLE node_capabilities ADD COLUMN core_opted_in INTEGER NOT NULL DEFAULT 0;
