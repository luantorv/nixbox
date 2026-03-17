{
  description = "NixBox - SandBoxed Agent Management Layer";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
    agent-sandbox = {
      url = "github:archie-judd/agent-sandbox.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, agent-sandbox }:
    let
      nixosModule = { config, lib, pkgs, ... }:
        let
          cfg = config.services.nixbox;
          python = pkgs.python3;
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
              description = "Path to the file containing API tokens, readable by the nixbox user.";
            };

            sandboxPackages = lib.mkOption {
              type = lib.types.attrsOf lib.types.package;
              default = { };
              description = "Attribute set of sandbox binaries produced by agent-sandbox.nix, keyed by sandbox type name.";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.nixbox = {
              isSystemUser = true;
              group = "nixbox";
              home = cfg.dataDir;
              createHome = true;
              description = "NixBox Service user";
            };

            users.groups.nixbox = { };

            systemd.services.nixbox = {
              description = "NixBox Agent Management Service";
              after = [ "network.target" ];
              wantedBy = [ "multi-user.target" ];

              environment = {
                NIXBOX_DATA_DIR = cfg.dataDir;
                NIXBOX_TOKEN_FILE = cfg.tokenFile;
                NIXBOX_SANDBOX_BINS = lib.concatStringsSep ","
                  (lib.mapAttrsToList (name: pkg: "${name}:${pkg}/bin/${name}") cfg.sandboxPackages);
                NIXBOX_HOST = cfg.host;
                NIXBOX_PORT = toString cfg.port;
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
        python = pkgs.python3;

        nixboxPkg = python.pkgs.buildPythonPackage {
          pname = "nixbox";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = with python.pkgs; [
            setuptools
          ];

          dependencies = with python.pkgs; [
            fastapi
            uvicorn
            sqlmodel
            apscheduler
            python-multipart
            aiofiles
          ];

          meta = {
            description = "Sandboxed agent management layer for NixOS";
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
              # desarrollo
              pytest
              pytest-asyncio
              httpx
            ]))
          ];
        };
      });
}
