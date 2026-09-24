# Designer model setup

The project defaults below were verified against the official APIs on 2026-09-24.

| Purpose | Model | API base | Provider / endpoint profile |
| --- | --- | --- | --- |
| Chat and planning | `deepseek-flash`, overridable with `DESIGN_DEEPSEEK_NAME` | `https://api.deepseek.com` | `OpenAI` / `deepseek` |
| Image generation | `image-01` | `https://api.minimaxi.com` | `MiniMax` / `minimax` |
| Video generation | `MiniMax-H3-Max` | `https://api.minimaxi.com` | `MiniMax` / `minimax` |

## Credentials and initialization

Export `DESIGN_DEEPSEEK_KEY` and `DESIGN_MINIMAX_KEY` in the shell that starts the
service. Keep their values outside the repository. Optionally export
`DESIGN_DEEPSEEK_NAME`; its verified default is `deepseek-flash`.

The bundled [environment template](../../../../resources/.env.template) maps these
variables to the application's `API_KEY`, `IMAGE_GEN_API_KEY` and
`VIDEO_GEN_API_KEY` settings. Both MiniMax capabilities use the same credential
source. The image and video model names are literal values in the bundled
[config.yaml](../../../../resources/config.yaml), under `models.image_gen` and
`models.video_gen`; edit these fields to change models. `DESIGN_MINIMAX_NAME`
is not used.

New instances initialized with `jiuwenswarm-init` use these templates. Image and
video generation are enabled in the template and require a valid MiniMax key.

## Update an existing instance

Pulling the repository does not rewrite an existing instance's configuration.
For the default instance, update only the relevant fields in
`~/.jiuwenswarm/config/.env`:

```dotenv
API_BASE=https://api.deepseek.com
API_KEY=${DESIGN_DEEPSEEK_KEY}
MODEL_NAME=${DESIGN_DEEPSEEK_NAME:-deepseek-flash}
MODEL_PROVIDER=OpenAI
ENDPOINT_PROFILE=deepseek

IMAGE_GEN_API_BASE=https://api.minimaxi.com
IMAGE_GEN_API_KEY=${DESIGN_MINIMAX_KEY}
IMAGE_GEN_PROVIDER=MiniMax
IMAGE_GEN_ENDPOINT_PROFILE=minimax
IMAGE_GEN_ENABLED=true

VIDEO_GEN_API_BASE=https://api.minimaxi.com
VIDEO_GEN_API_KEY=${DESIGN_MINIMAX_KEY}
VIDEO_GEN_PROVIDER=MiniMax
VIDEO_GEN_ENDPOINT_PROFILE=minimax
VIDEO_GEN_ENABLED=true
```

In `~/.jiuwenswarm/config/config.yaml`, update the selected default chat entry and
the two generation entries as follows. Preserve other settings and any additional
model entries; the intended chat entry should be the only one with `is_default: true`.

```yaml
models:
  defaults:
    - model_client_config:
        api_base: ${API_BASE}
        api_key: ${API_KEY}
        model_name: ${MODEL_NAME}
        client_provider: ${MODEL_PROVIDER}
        endpoint_profile: ${ENDPOINT_PROFILE:-deepseek}
      is_default: true
  image_gen:
    model_client_config:
      api_base: ${IMAGE_GEN_API_BASE}
      api_key: ${IMAGE_GEN_API_KEY}
      model_name: image-01
      client_provider: ${IMAGE_GEN_PROVIDER}
      endpoint_profile: ${IMAGE_GEN_ENDPOINT_PROFILE:-minimax}
  video_gen:
    model_client_config:
      api_base: ${VIDEO_GEN_API_BASE}
      api_key: ${VIDEO_GEN_API_KEY}
      model_name: MiniMax-H3-Max
      client_provider: ${VIDEO_GEN_PROVIDER}
      endpoint_profile: ${VIDEO_GEN_ENDPOINT_PROFILE:-minimax}
```

Keep the existing timeout and `model_config_obj` fields. Remove old
`IMAGE_GEN_MODEL_NAME` / `VIDEO_GEN_MODEL_NAME` definitions from the instance `.env`
to keep YAML as the source for MiniMax names. Restart the services from the shell
containing the exported credentials, then check the selected models in Settings.
For an isolated instance, apply the same changes under its configuration directory.

## Verification record

- DeepSeek: `GET /models` listed `deepseek-flash`; `POST /chat/completions` returned
  HTTP 200 with `OK` (10 tokens total), including Designer's thinking-disabled options.
- MiniMax image: `POST /v1/image_generation` with `image-01` produced a valid
  1280 × 720 JPEG.
- MiniMax video: `POST /v2/video_generation` with `MiniMax-H3-Max`, `480P`, 5 seconds
  and `16:9` reached `succeeded`; the downloaded MP4 decoded successfully
  (832 × 480, 24 fps, container duration 5.184 seconds).

These checks validate direct provider calls. Starting DesignSwarm and exercising
the full Designer workflow remain separate integration checks. Generated media,
credentials and signed download URLs are excluded from version control.

API references: [DeepSeek](https://api-docs.deepseek.com/),
[MiniMax images](https://platform.minimax.io/docs/api-reference/image-generation-t2i),
[MiniMax video V2](https://platform.minimax.io/docs/api-reference/video-generation-v2-create).
