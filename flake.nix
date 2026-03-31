{
  description = "NixBox - SandBoxed Agent Management Layer";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      nixosModule = { config, lib, pkgs, ... }:
        let
          cfg = config.services.nixbox;
          python = pkgs.python312;
          nixboxPkg = self.packages.${pkgs.system}.default;
        in
        {
          options.services.nixbox = {
            enable = lib.mkEnableOption "NixBox Agent Management Service";

            host = lib.mkOption {
              type = lib.types.str;
              default = "0.0.0.0";
              description = "Address to bind the HTTP server to.";
            };

            port = lib.mkOption {
              type = lib.types.port;
              default = 8000;
              description = "Port to bind the HTTP server to.";
            };

            dataDir = lib.mkOption {
              type = lib.types.path;
              default = "/var/lib/nixbox";
              description = "Directory for tasks, logs, and the SQLite database.";
            };

            tokenFile = lib.mkOption {
              type = lib.types.path;
              description = "Path to the file containing API tokens in KEY=VALUE format.";
            };

            sandboxProfiles = lib.mkOption {
              type = lib.types.attrsOf (lib.types.submodule {
                options = {
                  orchestratorModel = lib.mkOption {
                    type = lib.types.submodule {
                      options = {
                        provider = lib.mkOption { type = lib.types.str; };
                        model    = lib.mkOption { type = lib.types.str; };
                      };
                    };
                  };
                  executorModel = lib.mkOption {
                    type = lib.types.submodule {
                      options = {
                        provider = lib.mkOption { type = lib.types.str; };
                        model    = lib.mkOption { type = lib.types.str; };
                      };
                    };
                  };
                  allowedDomains   = lib.mkOption { type = lib.types.listOf lib.types.str; default = []; };
                  allowedActions   = lib.mkOption { type = lib.types.listOf lib.types.str; default = []; };
                  allowedLanguages = lib.mkOption {
                    type = lib.types.listOf lib.types.str;
                    default = [ "python" "javascript" ];
                  };
                };
              });
              default = {};
              description = "Sandbox profiles available to nixbox tasks.";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.nixbox = {
              isSystemUser = true;
              group = "nixbox";
              home = cfg.dataDir;
              createHome = true;
              description = "NixBox Service User";
            };

            users.groups.nixbox = {};

            systemd.services.nixbox = {
              description = "NixBox Agent Management Service";
              after = [ "network.target" ];
              wantedBy = [ "multi-user.target" ];

              environment = {
                NIXBOX_DATA_DIR   = cfg.dataDir;
                NIXBOX_TOKEN_FILE = cfg.tokenFile;
                NIXBOX_HOST       = cfg.host;
                NIXBOX_PORT       = toString cfg.port;
                NIXBOX_PROFILES   = builtins.toJSON (
                  lib.mapAttrs (_: p: {
                    orchestrator_model = { inherit (p.orchestratorModel) provider model; };
                    executor_model     = { inherit (p.executorModel) provider model; };
                    allowed_domains    = p.allowedDomains;
                    allowed_actions    = p.allowedActions;
                    allowed_languages  = p.allowedLanguages;
                  }) cfg.sandboxProfiles
                );
              };

              serviceConfig = {
                User = "nixbox";
                Group = "nixbox";
                WorkingDirectory = cfg.dataDir;
                ExecStart = "${nixboxPkg}/bin/nixbox";
                ReadOnlyPaths = [ cfg.tokenFile ];
                ReadWritePaths = [ cfg.dataDir ];
                NoNewPrivileges = true;
                PrivateTmp = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                Restart = "on-failure";
                RestartSec = "5s";
              };
            };
          };
        };
    in
    {
      nixosModules.default = nixosModule;
    }
    //
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        # google-genai no está en nixpkgs 25.11, se empaqueta desde PyPI
        google-genai = python.pkgs.buildPythonPackage rec {
          pname = "google_genai";
          version = "1.10.0";
          pyproject = true;
          build-system = with python.pkgs; [ setuptools ];
          src = python.pkgs.fetchPypi {
            inherit pname version;
            hash = "sha256-9ZQj4PFV3Ga3eSyKDmckx1xy3GmdHreQfU0ABtT2GG8=";
          };
          dependencies = with python.pkgs; [
            anyio
            google-auth
            httpx
            pydantic
            requests
            typing-extensions
            websockets
          ];
          doCheck = false;
        };

        nixboxPkg = python.pkgs.buildPythonPackage {
          pname = "nixbox";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = with python.pkgs; [ setuptools ];
          dependencies = with python.pkgs; [
            fastapi
            uvicorn
            sqlmodel
            apscheduler
            python-multipart
            aiofiles
            aiosqlite
            psutil
            httpx
            anthropic
            openai
            markdown-it-py
            google-genai
          ];

          meta = {
            description = "Sandboxed Agent Management Layer for NixOS";
            license = pkgs.lib.licenses.mit;
          };
        };
      in
      {
        packages.default = nixboxPkg;

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (ps: with ps; [
              fastapi
              uvicorn
              sqlmodel
              apscheduler
              python-multipart
              aiofiles
              aiosqlite
              psutil
              httpx
              anthropic
              openai
              markdown-it-py
              google-genai
              pytest
              pytest-asyncio
            ]))
            pkgs.nodejs_22
          ];
        };
      });
}
