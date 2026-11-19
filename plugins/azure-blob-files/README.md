# codee-azure-blob-files

A [Codee](https://github.com/fusebase-dev/codee) plugin that serves the files of a
**private** Azure Blob container on the admin UI, at `/p/files/<path>`. It works like an
S3 static website, but it is not public: anyone who can open the admin UI can open the
files, and nobody else. Put the admin UI behind a sign-in proxy (for example
oauth2-proxy with Entra ID) to limit it to your organization.

- A folder URL (`/p/files/report/`) serves its `index.html`.
- `/p/files/report` with no slash redirects to `/p/files/report/`.
- There is no CDN and no caching, so a file you upload again shows at once.

## Install

In your Codee project:

```bash
uv add codee-azure-blob-files
```

Restart Codee. **Azure Blob files** appears on the **Plugins** page, turned **off**.
Fill in its settings, then turn it on.

## Set up

1. Create a storage account and a container, with public access off:

   ```bash
   az storage account create -n <account> -g <group> --sku Standard_LRS \
     --kind StorageV2 --allow-blob-public-access false
   az storage container create --account-name <account> -n files --auth-mode login
   ```

2. Give the identity Codee runs as (`az login` user or service principal, or a managed
   identity) **Storage Blob Data Contributor** on the account. Reader is enough
   to serve the files. To upload, the identity needs Contributor.
3. On the Plugins page, fill in the account URL (`https://<account>.blob.core.windows.net`)
   and the container. Leave **Connection string** empty to use that identity.

## Upload

One folder at a time:

```bash
az storage blob upload-batch --auth-mode login --account-name <account> \
  -d files/<folder> -s ./my-folder
```

Then open `<admin URL>/p/files/<folder>/`.
