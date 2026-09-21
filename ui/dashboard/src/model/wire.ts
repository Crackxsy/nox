/**
 * The bridge between the generated IPC contract and this app's view models.
 *
 * `ui/shared/generated/ipc.ts` is regenerated from `src/nox/ipc/protocol.py` and checked in CI, so
 * it is the truth about what the core sends: snake_case, optional where pydantic says optional.
 * The screens want camelCase and no optionals. Both are legitimate; what is not legitimate is the
 * two drifting apart silently, which is how `clip.export` came to render "Export fertig: undefined".
 *
 * `FieldMap` is the tie. Every parser that mirrors a generated interface declares one, listing each
 * wire field and the view field it becomes. It costs nothing at runtime and fails the build in both
 * directions: rename a field in the protocol and the table has a key the wire type no longer has;
 * rename a field in the view model and the table's value is no longer a key of it.
 */

/** One entry per field of `Wire`, naming the field of `View` it maps onto. */
export type FieldMap<Wire, View> = { readonly [K in keyof Required<Wire>]: keyof View };
