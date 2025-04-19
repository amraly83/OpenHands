#!/bin/bash
set -eo pipefail

echo "Starting OpenHands..."

# Normalize workspace paths
if [ -n "$WORKSPACE_BASE" ]; then
  # Evaluate any $PWD or other variables in the path
  WORKSPACE_BASE=$(eval echo "$WORKSPACE_BASE")
  
  # Remove any leading dash and trailing brace
  WORKSPACE_BASE=$(echo "$WORKSPACE_BASE" | sed 's/^-\/*//;s/}$//')
  
  # Ensure path starts with /
  if [[ "$WORKSPACE_BASE" != /* ]]; then
    WORKSPACE_BASE="/$WORKSPACE_BASE"
  fi
  
  # Set the normalized paths
  export WORKSPACE_BASE
  export WORKSPACE_MOUNT_PATH="$WORKSPACE_BASE"
fi

# Normalize OpenHands state path
if [ -n "$OPENHANDS_STATE_PATH" ]; then
  # Evaluate any ~ or other variables in the path
  OPENHANDS_STATE_PATH=$(eval echo "$OPENHANDS_STATE_PATH")
  
  # Remove any leading dash and trailing brace
  OPENHANDS_STATE_PATH=$(echo "$OPENHANDS_STATE_PATH" | sed 's/^-\/*//;s/}$//')
  
  # Ensure path starts with /
  if [[ "$OPENHANDS_STATE_PATH" != /* ]]; then
    OPENHANDS_STATE_PATH="/$OPENHANDS_STATE_PATH"
  fi
  
  export OPENHANDS_STATE_PATH
fi

if [[ $NO_SETUP == "true" ]]; then
  echo "Skipping setup, running as $(whoami)"
  "$@"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "The OpenHands entrypoint.sh must run as root"
  exit 1
fi

if [ -z "$SANDBOX_USER_ID" ]; then
  echo "SANDBOX_USER_ID is not set"
  exit 1
fi

if [ -z "$WORKSPACE_MOUNT_PATH" ]; then
  # This is set to /opt/workspace in the Dockerfile. But if the user isn't mounting, we want to unset it so that OpenHands doesn't mount at all
  unset WORKSPACE_BASE
fi

if [[ "$SANDBOX_USER_ID" -eq 0 ]]; then
  echo "Running OpenHands as root"
  export RUN_AS_OPENHANDS=false
  mkdir -p /root/.cache/ms-playwright/
  if [ -d "/home/openhands/.cache/ms-playwright/" ]; then
    mv /home/openhands/.cache/ms-playwright/ /root/.cache/
  fi
  "$@"
else
  echo "Setting up enduser with id $SANDBOX_USER_ID"
  if id "enduser" &>/dev/null; then
    echo "User enduser already exists. Skipping creation."
  else
    if ! useradd -l -m -u $SANDBOX_USER_ID -s /bin/bash enduser; then
      echo "Failed to create user enduser with id $SANDBOX_USER_ID. Moving openhands user."
      incremented_id=$(($SANDBOX_USER_ID + 1))
      usermod -u $incremented_id openhands
      if ! useradd -l -m -u $SANDBOX_USER_ID -s /bin/bash enduser; then
        echo "Failed to create user enduser with id $SANDBOX_USER_ID for a second time. Exiting."
        exit 1
      fi
    fi
  fi
  usermod -aG app enduser
  # get the user group of /var/run/docker.sock and set openhands to that group
  DOCKER_SOCKET_GID=$(stat -c '%g' /var/run/docker.sock)
  echo "Docker socket group id: $DOCKER_SOCKET_GID"
  if getent group $DOCKER_SOCKET_GID; then
    echo "Group with id $DOCKER_SOCKET_GID already exists"
  else
    echo "Creating group with id $DOCKER_SOCKET_GID"
    groupadd -g $DOCKER_SOCKET_GID docker
  fi

  mkdir -p /home/enduser/.cache/huggingface/hub/
  mkdir -p /home/enduser/.cache/ms-playwright/
  if [ -d "/home/openhands/.cache/ms-playwright/" ]; then
    mv /home/openhands/.cache/ms-playwright/ /home/enduser/.cache/
  fi

  usermod -aG $DOCKER_SOCKET_GID enduser
  echo "Running as enduser"
  su enduser /bin/bash -c "${*@Q}" # This magically runs any arguments passed to the script as a command
fi
