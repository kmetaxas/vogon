{
  description = "Vogon native development environment";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      environment = system:
        let
          pkgs = import nixpkgs { inherit system; };
          postgres = pkgs.postgresql_16.withPackages (p: [ p.pgvector ]);
          helpers = pkgs.symlinkJoin {
            name = "vogon-dev-helpers";
            paths = map (name: pkgs.writeShellApplication {
              inherit name;
              runtimeInputs = [ pkgs.uv pkgs.python311 postgres pkgs.temporal-cli pkgs.coreutils ];
              text = builtins.readFile (./nix + "/${name}.sh");
            }) [ "vogon-dev-sync" "vogon-postgres" "vogon-temporal" ];
          };
        in { inherit pkgs postgres helpers; };
    in {
      packages = eachSystem (system: { default = (environment system).helpers; });
      checks = eachSystem (system:
        let inherit (environment system) pkgs helpers;
        in {
          inherit helpers;
          syntax = pkgs.runCommand "vogon-dev-script-syntax" { nativeBuildInputs = [ pkgs.shellcheck ]; } ''
            for script in ${./nix}/*.sh; do
              bash -n "$script"
              shellcheck --shell=bash "$script"
            done
            touch "$out"
          '';
        });
      devShells = eachSystem (system:
        let inherit (environment system) pkgs postgres helpers;
        in { default = pkgs.mkShell {
          packages = [ pkgs.python311 pkgs.uv pkgs.gnumake pkgs.stdenv.cc pkgs.pkg-config
            pkgs.libffi pkgs.openssl postgres pkgs.temporal-cli pkgs.zsh helpers ];
          UV_PYTHON = "${pkgs.python311}/bin/python3.11";
          UV_PYTHON_DOWNLOADS = "never";
          UV_NO_MANAGED_PYTHON = "1";
          UV_LOCKED = "1";
          UV_NO_BUILD_ISOLATION_PACKAGE = "vogon";
          UV_PROJECT_ENVIRONMENT = ".venv";
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib pkgs.openssl ];
          shellHook = ''
            export VOGON_DEV_ROOT="$PWD"
            export UV_NO_BUILD_PACKAGE
            UV_NO_BUILD_PACKAGE=$("$UV_PYTHON" -c 'import tomllib; print(" ".join(p["name"] for p in tomllib.load(open("uv.lock", "rb"))["package"] if "registry" in p["source"]))') || exit 1
            if ! vogon-dev-sync; then
              echo "Vogon environment synchronization failed; refusing an incomplete shell." >&2
              exit 1
            fi
            source "$VOGON_DEV_ROOT/.venv/bin/activate"
          '';
        }; });
    };
}
