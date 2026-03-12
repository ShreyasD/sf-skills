# Complete Agent Templates

Templates for building complete, deployable agents.

## Learning Path

### Service Agent Examples
| Template | Complexity | Description |
|----------|------------|-------------|
| `hello-world.agent` | Beginner | Minimal viable Service Agent - start here |
| `simple-qa.agent` | Beginner | Single-topic Q&A agent |
| `multi-topic.agent` | Intermediate | Multi-topic routing agent |
| `production-faq.agent` | Advanced | Production-ready FAQ with escalation |

### Employee Agent Examples
| Template | Complexity | Description |
|----------|------------|-------------|
| `hello-world-employee.agent` | Beginner | Minimal viable Employee Agent - no dedicated user needed |

> **Service vs Employee**: Service Agents run as a dedicated Einstein Agent User and require `default_agent_user`, linked Messaging variables, and `connection` blocks. Employee Agents run as the logged-in user and need none of these. See [agent-user-setup.md](../../references/agent-user-setup.md) for details.

## Quick Start

1. Copy a template to your SFDX project:
   ```bash
   mkdir -p force-app/main/default/aiAuthoringBundles/My_Agent
   cp hello-world.agent force-app/main/default/aiAuthoringBundles/My_Agent/My_Agent.agent
   cp ../metadata/bundle-meta.xml force-app/main/default/aiAuthoringBundles/My_Agent/My_Agent.bundle-meta.xml
   ```

2. Validate and deploy:
   ```bash
   sf agent validate authoring-bundle --api-name My_Agent --target-org your-org --json
   sf agent publish authoring-bundle --api-name My_Agent --target-org your-org --json
   ```

## Required Blocks

Every agent must have these blocks **in this order**:

| Block | Purpose |
|-------|---------|
| `system:` | Agent personality and default messages |
| `config:` | Deployment metadata (agent_name, label, etc.) |
| `variables:` | Data connections and state storage |
| `language:` | Locale configuration |
| `start_agent` | Entry point topic (exactly one required) |

## Next Steps

- [components/](../components/) - Reusable action and topic templates
- [patterns/](../patterns/) - Advanced patterns for complex behaviors
- [metadata/](../metadata/) - XML metadata templates
