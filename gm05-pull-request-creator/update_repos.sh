#!/bin/bash

# Repository list
repos=()
while IFS= read -r line; do
    repos+=("$line")
done < "repositories-list.txt"

# Template path
script_dir=$(dirname "$(realpath "$0")")
template_path="$script_dir/jira-issue-required.yml"
issue_key=""

reset(){
    for repo in "${repos[@]}"; do
        repo_name=$(basename "$repo" .git)
        echo "Removing directory $repo_name"
        rm -rf "$repo_name"
    done
}


# Get Jira Issue Key from user input
get_user_input() {
    read -p "Enter the Jira issue key for creating branches in the repositories to be updated: " issue_key
    echo $issue_key
}

# Update repository
update_repo() {

    repo_url=$1
    repo_name=$(basename "$repo_url" .git)

    # Clone the repository
    git clone "$repo_url"

    # Enter the repository directory
    cd "$repo_name" || exit

    # Check if the file exists, if not, create the directories and the file
    if [ ! -f ".github/workflows/jira-issue-required.yml" ]; then
        mkdir -p .github/workflows
    fi

    # Remove all YAML files with 'jira' prefix in the .github/workflows folder
    find .github/workflows -type f \( -name 'jira*.yml' -o -name 'jira*.yaml' \) -exec rm -f {} +
    cp "$template_path" .github/workflows/jira-issue-required.yml

    # Replace main branch in the file
    main_branch=$(git remote show origin | grep 'HEAD branch' | cut -d' ' -f5)
    sed -i "s/MAIN_BRANCH/$main_branch/" .github/workflows/jira-issue-required.yml

    # Create a new branch with the issue key name
    git pull
    git checkout -b "$issue_key"
    git add .
    git commit -am "Adicionando arquivo de configuração para checagem de issue key"
    git push origin "$issue_key"

    # Create pull request
    pr_title="[$issue_key] GM05 Configuração para checagem de issue key no status de deploy"
    pr_body=$(cat <<EOF
# Ticket
[$issue_key](https://afya-spm.atlassian.net/browse/$issue_key)

# Description
Configuração para checagem de issue key no status de deploy segundo o template do GM05.
EOF
)
    pr_url=$(gh pr create --title "$pr_title" --body "$pr_body" --base "$main_branch" --head "$issue_key")
    echo "Pull request created: $pr_url"
    pr_urls+=("$pr_url")


    # Go back to the previous directory
    cd ..

    echo "Repository $repo_name updated."
}


reset

get_user_input

# Iterate over the list of repositories
for repo in "${repos[@]}"; do
    update_repo "$repo"
done

echo "All repositories have been updated."
echo "Links for the pull requests:"
for pr_url in "${pr_urls[@]}"; do
    echo "$pr_url"
done
