# agent-container

AI coding agentsをホスト環境から分離して動かす、Linux・rootless Podman向けの開発環境です。

Current release: `v0.1.0`

現在はPhase 2まで実装済みの初期公開版です。`0.x`の間はCLI、state配置、security boundaryが互換性なく変更される可能性があります。

- [Phase 1設計](docs/superpowers/specs/2026-08-22-phase-1-codex-container-design.md)
- [Phase 1 operator guide](docs/phase1-codex-container.md)
- [Phase 1実host smoke test](docs/phase1-smoke-test.md)
- [Phase 2 Claude Code設計](docs/superpowers/specs/2026-08-23-phase-2-claude-code-design.md)
- [Phase 2 Claude Code operator guide](docs/phase2-claude-code.md)
- [Phase 2 Claude Code実host smoke test](docs/phase2-smoke-test.md)
- [PR workflow](docs/phase1-codex-container.md#日常の運用)
- [Changelog](CHANGELOG.md)

versionは次のcommandで確認できます。

```bash
bin/agentctl --version
```

## License

GNU General Public License v3.0。詳細は[LICENSE](LICENSE)を参照してください。
