# Runpod API notes

Runpod exposes two APIs. `rpt` uses both because neither covers everything,
and each has a trap the other does not.

| Need | API | Endpoint / operation | Why this one |
|---|---|---|---|
| List pods with GPU name, uptime, ports | GraphQL | `myself { pods { ... machine { gpuDisplayName } runtime { ports } } }` | REST's pod object lacks the GPU display name and the runtime port map, which is where the ssh `ip:port` lives. |
| Create a pod | REST | `POST /v1/pods` | Documented, accepts `templateId`, `imageName`, `supportPublicIp`, `networkVolumeId`. GraphQL `podFindAndDeployOnDemand` works as a fallback when REST is down. |
| Stop a pod | REST | `POST /v1/pods/{id}/stop` | Simple, documented. |
| Resume a pod | GraphQL | `podResume(input: {podId, gpuCount})` | Needs the GPU count; the REST start endpoint has been unreliable for stopped pods. |
| Terminate a pod | GraphQL | `podTerminate(input: {podId})` | Returns nothing on success; `rpt` treats "no errors" as done. REST `DELETE /v1/pods/{id}` also works (used by `examples/killswitch.sh` because curl is easier there). |
| GPU types and prices | GraphQL | `gpuTypes { id displayName memoryInGb secureCloud communityCloud lowestPrice(input:{gpuCount:1}) {...} }` | No REST equivalent. |
| Network volumes | REST | `GET /v1/networkvolumes` | Simple list. **There is deliberately no delete in this tool.** |
| Read a template | GraphQL | `myself { podTemplates { id name imageName ports containerDiskInGb volumeInGb volumeMountPath dockerArgs env { key value } } }` | Returns `{{ RUNPOD_SECRET_* }}` placeholders verbatim. REST `GET /v1/templates/{id}` resolves them to secret values; round-tripping that through a write would inline the secrets. |
| Update template env / image | REST | `PATCH /v1/templates/{id}` with `{"env": {...}, "imageName": ...}` | Partial update; env is sent as a full merged map. |
| Update template ports / volume size | GraphQL | `saveTemplate(input: SaveTemplateInput!)` | Not PATCHable via REST. Needs the *full* template object (id, name, imageName, ports, containerDiskInGb, volumeInGb, dockerArgs, env list, volumeMountPath) or the omitted fields reset. |

Base URLs: REST `https://rest.runpod.io/v1`, GraphQL
`https://api.runpod.io/graphql`. Both take `Authorization: Bearer <key>`.

## Shapes worth knowing

**Pod (GraphQL)**: `desiredStatus` is `RUNNING`, `EXITED` (stopped),
`CREATED` (starting), or `TERMINATED`. `runtime` is `null` while starting and
for stopped pods. `runtime.ports[]` entries have `privatePort`, `publicPort`,
`ip`, `type`; the ssh endpoint is the entry with `privatePort == 22`.
`costPerHr` is the current rate; there is no cumulative cost field, `rpt`
multiplies rate by uptime.

**Create response (REST)**: `id`, `name`, `costPerHr`, `machine.gpuTypeId`,
`machine.dataCenterId`. No ports yet; poll the GraphQL listing.

**Errors**: REST returns non-2xx with `{"error": "..."}`; "no instances
available" style capacity errors are 4xx and worth retrying. GraphQL returns
HTTP 200 with an `errors` array; `rpt` turns that into a failure.

**Secrets**: created only in the web console (Settings → Secrets). Referenced
from template env as `{{ RUNPOD_SECRET_<Name> }}`; the name is case-sensitive.
